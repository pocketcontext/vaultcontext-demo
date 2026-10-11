"""Synthetic local demo lifecycle/contact retention checks; no external services."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import socket
import subprocess
import sys
from datetime import datetime, timezone
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    spec = importlib.util.spec_from_file_location('demo_' + name, ROOT / 'deploy/demo' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

reset = load('reset')
retention = load('retention')
with patch.dict(sys.modules, {'reset':reset}):
    local = load('local_lifecycle')


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'vaultcontext-demo'
        self.root.mkdir(mode=0o700)
        (self.root / 'runtime/pb_data').mkdir(parents=True)
        (self.root / 'runtime/pb_data/data.db').write_bytes(b'synthetic-db')
        (self.root / 'runtime/pb_data/auxiliary.db').write_bytes(b'synthetic-aux')
        (self.root / 'persistent').mkdir()
        (self.root / 'persistent/contacts.db').write_bytes(b'keep-synthetic')
        self.cfg = {'deployment': 'vaultcontext-demo', 'origin': 'http://127.0.0.1:8769',
                    'root': str(self.root), 'storage': {'kind': 'local'},
                    'hooks': {name: [sys.executable, '-c', 'raise SystemExit(0)'] for name in ('fence','stop','assert_stopped','initialize','start','health','unfence')}}
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps(self.cfg))
        self.config.chmod(0o600)

    def test_full_reset_preserves_persistent_store_and_is_idempotent(self):
        cfg = reset.configuration(self.config)
        result = reset.reset(cfg, '2026-10-10')
        self.assertTrue(result['reset'])
        self.assertFalse((self.root / 'runtime/pb_data').exists())
        self.assertEqual((self.root / 'persistent/contacts.db').read_bytes(), b'keep-synthetic')
        self.assertTrue(reset.reset(cfg, '2026-10-10')['alreadyComplete'])
        self.assertFalse((self.root / 'control/reset-pending.json').exists())

    def test_failure_retains_fence_and_resume(self):
        self.cfg['hooks']['health'] = [sys.executable, '-c', 'raise SystemExit(1)']
        with self.assertRaises(reset.ResetError):
            reset.reset(self.cfg, '2026-10-10')
        self.assertTrue((self.root / 'control/reset-pending.json').exists())
        with self.assertRaises(reset.ResetError):
            reset.reset(self.cfg, '2026-10-11')
        self.cfg['hooks']['health'] = [sys.executable, '-c', 'raise SystemExit(0)']
        self.assertTrue(reset.reset(self.cfg, '2026-10-10')['reset'])

    def test_stop_failure_never_deletes(self):
        self.cfg['hooks']['assert_stopped'] = [sys.executable, '-c', 'raise SystemExit(1)']
        with self.assertRaises(reset.ResetError):
            reset.reset(self.cfg, '2026-10-10')
        self.assertTrue((self.root / 'runtime/pb_data/data.db').exists())

    def test_reject_production_and_symlink(self):
        self.cfg['origin'] = 'https://vault.pocketcontext.com'
        self.config.write_text(json.dumps(self.cfg))
        with self.assertRaises(reset.ResetError):
            reset.configuration(self.config)
        (self.root / 'control').symlink_to(self.root / 'persistent')
        with self.assertRaises(reset.ResetError):
            reset.reset(self.cfg, '2026-10-10')

    def test_migration_fence_is_rechecked_after_lifecycle_lock(self):
        original=reset.fcntl.flock
        def acquire(descriptor,flags):
            result=original(descriptor,flags)
            reset.atomic_json(self.root/'control/migration-fenced.json',{'phase':'source-fenced'})
            return result
        with patch.object(reset.fcntl,'flock',side_effect=acquire):
            with self.assertRaisesRegex(reset.ResetError,'migration fence'):
                reset.reset(self.cfg,'2026-10-10')
        self.assertTrue((self.root/'runtime/pb_data/data.db').exists())
        self.assertFalse((self.root/'control/reset-pending.json').exists())

    def test_dry_run_does_not_delete(self):
        self.assertTrue(reset.reset(self.cfg, '2026-10-10', True)['dryRun'])
        self.assertTrue((self.root / 'runtime/pb_data/data.db').exists())

    def test_pending_launch_never_means_stopped(self):
        (self.root/'control').mkdir()
        (self.root/'control/launch-pending.json').write_text('{}')
        with self.assertRaises(reset.ResetError):
            local.running(self.root)

    def test_unknown_listener_refuses_stopped_assertion(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen(1)
            with self.assertRaises(reset.ResetError):
                local.assert_port_free({'port':listener.getsockname()[1]})

    def test_local_process_cannot_inherit_remote_storage_credentials(self):
        with patch.dict(os.environ, {'VAULTCONTEXT_S3_BUCKET':'synthetic-prod',
                       'LITESTREAM_ENDPOINT':'https://synthetic.invalid','AWS_SECRET_ACCESS_KEY':'synthetic',
                       'VAULTCONTEXT_DEMO_CONTACT_TOKEN':'synthetic-local'}):
            env = local.local_environment()
            self.assertFalse(any(key.startswith(('VAULTCONTEXT_S3_','LITESTREAM_','AWS_')) for key in env))
            self.assertEqual(env['VAULTCONTEXT_DEMO_CONTACT_TOKEN'],'synthetic-local')

    def test_s3_delete_error_fails_closed(self):
        class Client:
            def delete_objects(self, **kwargs):
                return {'Errors': [{'Code': 'AccessDenied'}]}
        with self.assertRaises(reset.ResetError):
            reset.S3Bucket('synthetic', '', Client()).delete([{'Key':'fixture'}])


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = retention.Store(Path(self.temp.name) / 'contacts.db')
        self.body = {'subject':'synthetic-google-sub', 'email':'synthetic@example.test',
                     'salesContact':False, 'newsletter':False, 'termsVersion':'demo-2026-10-09',
                     'consentVersion':'demo-2026-10-09','expectedRevision':0}

    def test_no_consent_purges_after_last_use_not_signup(self):
        self.store.enroll(self.body, now=1000)
        self.store.touch(self.body['subject'], now=2000)
        self.assertEqual(self.store.maintenance(1000+30*retention.DAY)['expiredNonmarketing'], 0)
        self.assertEqual(self.store.maintenance(2000+30*retention.DAY)['expiredNonmarketing'], 1)
        self.assertEqual(self.store.preferences(self.body['subject'])['revision'], 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM consent_events').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM suppression').fetchone()[0], 0)

    def test_opt_in_review_does_not_delete(self):
        self.body['newsletter'] = True
        result = self.store.enroll(self.body, now=1000)
        stats = self.store.maintenance(result['reviewDue'])
        self.assertEqual(stats, {'expiredNonmarketing':0,'marketingReviewDue':1})
        self.assertTrue(self.store.preferences(self.body['subject'])['newsletter'])

    def test_withdrawal_stale_revision_and_touch(self):
        self.body['newsletter'] = True
        result = self.store.enroll(self.body, now=1000)
        self.store.unsubscribe(result['unsubscribeToken'], now=2000)
        self.store.touch(self.body['subject'], now=3000)
        self.assertFalse(self.store.preferences(self.body['subject'])['newsletter'])
        self.body['expectedRevision'] = 1
        with self.assertRaises(retention.Rejected) as failure:
            self.store.enroll(self.body, now=4000)
        self.assertEqual(failure.exception.status, 409)
        self.store.maintenance(3000+30*retention.DAY)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT newsletter FROM suppression').fetchone()[0], 1)

    def test_security_log_retention_and_incident_hold(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO security_events(at,event,incident_until) VALUES (1000,?,0)', ('synthetic',))
            db.execute('INSERT INTO security_events(at,event,incident_until) VALUES (1000,?,?)', ('synthetic-hold',1000+40*retention.DAY))
        self.store.maintenance(1000+30*retention.DAY)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM security_events').fetchone()[0],1)
        self.store.maintenance(1000+40*retention.DAY)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM security_events').fetchone()[0],0)

    def test_operator_review_retain_is_revision_checked_and_audited(self):
        self.body['newsletter'] = True
        first = self.store.enroll(self.body, now=1000)
        decision = {'subject':self.body['subject'],'expectedRevision':1,'decision':'retain','reason':'Current product evaluation; continued need reviewed'}
        result = self.store.review(decision, now=first['reviewDue'])
        self.assertEqual(result['revision'],2)
        self.assertGreater(result['reviewDue'],first['reviewDue'])
        with self.assertRaises(retention.Rejected) as failure:
            self.store.review(decision, now=first['reviewDue']+1)
        self.assertEqual(failure.exception.status,409)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM review_events').fetchone()
            self.assertEqual(row['reason'],decision['reason'])
            self.assertEqual(row['at'],first['reviewDue'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM suppression').fetchone()[0],0)

    def test_operator_delete_removes_evidence_and_preserves_suppression(self):
        self.body['newsletter'] = True
        first = self.store.enroll(self.body, now=1000)
        self.store.review({'subject':self.body['subject'],'expectedRevision':1,'decision':'retain','reason':'Synthetic justified review'}, now=2000)
        self.store.unsubscribe(first['unsubscribeToken'],now=3000)
        with self.store.connect() as db:
            before = tuple(db.execute('SELECT * FROM suppression').fetchone())
        result = self.store.review({'subject':self.body['subject'],'expectedRevision':3,'decision':'delete','reason':'Verified privacy removal request'}, now=4000)
        self.assertTrue(result['deleted'])
        with self.store.connect() as db:
            for table in ('contacts','consent_events','review_events','unsubscribe_tokens'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
            self.assertEqual(tuple(db.execute('SELECT * FROM suppression').fetchone()),before)

    def test_operator_queue_and_review_cannot_create_consent(self):
        self.store.enroll(self.body,now=1000)
        with self.assertRaises(retention.Rejected):
            self.store.review({'subject':self.body['subject'],'expectedRevision':1,'decision':'retain','reason':'No consent cannot be renewed'},now=2000)
        self.body['newsletter']=True
        result=self.store.enroll({**self.body,'expectedRevision':1},now=3000)
        queue=self.store.review_queue({'after':'','limit':100},now=result['reviewDue'])
        self.assertEqual(queue['items'],[{'subject':self.body['subject'],'revision':2,'reviewDue':result['reviewDue']}])
        self.assertNotIn('email',queue['items'][0])
        self.store.review({'subject':self.body['subject'],'expectedRevision':2,'decision':'delete','reason':'Evaluation no longer relevant'},now=4000)
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM suppression').fetchone()
            self.assertEqual((row['sales'],row['newsletter']),(1,1))

    def test_enrollment_observation_is_atomic_with_preferences(self):
        body = {**self.body, 'security': {'event':'enrollment_request','ip':'192.0.2.5','timestamp':1000}}
        with patch.object(self.store, 'record_security', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                self.store.enroll(body, now=1000)
        self.assertEqual(self.store.preferences(self.body['subject'])['revision'], 0)
        with self.store.connect() as db:
            for table in ('contacts', 'consent_events', 'unsubscribe_tokens', 'security_events'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)
        self.store.enroll(body, now=1000)
        self.store.enroll({**body, 'expectedRevision':1}, now=1001)
        with self.store.connect() as db:
            events = db.execute('SELECT event,ip FROM security_events').fetchall()
            self.assertEqual([tuple(row) for row in events], [('enrollment_request','192.0.2.5')])
        with self.assertRaises(retention.Rejected):
            self.store.enroll({**body, 'expectedRevision':2, 'security':{**body['security'], 'event':'auth_success'}}, now=1002)
        self.assertEqual(self.store.preferences(self.body['subject'])['revision'], 2)

    def test_unsubscribe_token_length_boundaries(self):
        for token in [*('x'*length for length in (20,31,129,256)), '\U0001f600'*16, '\u00e9'*32, 'x'*32+'\n', ' '*32, '/'*32]:
            with self.assertRaises(retention.Rejected) as failure:
                self.store.unsubscribe(token)
            self.assertEqual(failure.exception.status, 400)
        for length in (32, 128):
            self.assertEqual(self.store.unsubscribe('x'*length), {'ok':True})

    def test_security_validation_and_deduplication(self):
        body = {'event':'enrollment','ip':'2001:db8::1','timestamp':1000}
        self.store.security(body, now=1000)
        self.store.security(body, now=1001)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM security_events').fetchone()[0],1)
        for changes in ({'ip':'not-an-ip'}, {'ip':'fe80::1%eth0'}, {'timestamp':0}, {'event':'raw-sensitive-body'}):
            with self.assertRaises(retention.Rejected):
                self.store.security({**body,**changes}, now=1000)

    def test_old_unsubscribe_capability_survives_preference_update(self):
        self.body['newsletter'] = True
        first = self.store.enroll(self.body, now=1000)
        self.store.enroll({**self.body,'expectedRevision':1,'salesContact':True}, now=2000)
        self.store.unsubscribe(first['unsubscribeToken'], now=3000)
        result = self.store.preferences(self.body['subject'])
        self.assertFalse(result['salesContact'])
        self.assertFalse(result['newsletter'])

    def test_http_auth_and_unsubscribe_capability(self):
        token = 's' * 40
        server = ThreadingHTTPServer(('127.0.0.1',0),retention.handler(self.store,token))
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port)
        request = Request(url+'/enroll',json.dumps(self.body).encode(), {'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as result:
            urlopen(request)
        self.assertEqual(result.exception.code,401)
        result.exception.close()
        request.add_header('Authorization','Bearer '+token)
        with urlopen(request) as response:
            body = json.load(response)
        with urlopen(Request(url+'/unsubscribe',json.dumps({'token':body['unsubscribeToken']}).encode(), {'Content-Type':'application/json'})) as response:
            self.assertEqual(response.status,200)
        with self.assertRaises(HTTPError) as error:
            urlopen(url+'/unsubscribe?token='+body['unsubscribeToken'])
        error.exception.close()


class LocalServerResetTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('VAULTCONTEXT_DEMO_TEST_BINARY'), 'set pinned binary to run concrete local lifecycle')
    def test_pinned_server_full_reset(self):
        with tempfile.TemporaryDirectory(prefix='demo-lifecycle-test-') as tmp:
            root = Path(tmp) / 'vaultcontext-demo'
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                port = listener.getsockname()[1]
            token = 'synthetic-contact-secret-' + 'x'*32
            script = ROOT/'deploy/demo/local_lifecycle.py'
            subprocess.run([sys.executable,str(script),'configure','--root',str(root),'--binary',os.environ['VAULTCONTEXT_DEMO_TEST_BINARY'],'--port',str(port)],check=True)
            store = retention.Store(root/'persistent/contacts.db')
            store.enroll({'subject':'survives','email':'synthetic@example.test','salesContact':True,'newsletter':False,
                          'termsVersion':'demo-2026-10-09','consentVersion':'demo-2026-10-09','expectedRevision':0})
            http = ThreadingHTTPServer(('127.0.0.1',0),retention.handler(store,token))
            thread = threading.Thread(target=http.serve_forever,daemon=True)
            thread.start()
            env = {'VAULTCONTEXT_DEMO_CONTACT_URL':'http://127.0.0.1:'+str(http.server_port),
                   'VAULTCONTEXT_DEMO_CONTACT_TOKEN':token}
            day = datetime.now(timezone.utc).date().isoformat()
            try:
                with patch.dict(os.environ,env):
                    config = reset.configuration(root/'control/reset.json')
                    self.assertTrue(reset.reset(config,day)['reset'])
                    self.assertTrue((root/'runtime/pb_data/data.db').is_file())
                    self.assertTrue((root/'runtime/pb_data/auxiliary.db').is_file())
                    self.assertTrue(store.preferences('survives')['salesContact'])
                    with urlopen('http://127.0.0.1:'+str(port)+'/api/demo/status') as response:
                        self.assertEqual(json.load(response)['generation'],day)
                    # Populate an orphan and an auxiliary sentinel, then force the same
                    # completed generation into an interrupted-reset fixture to resume it.
                    (root/'runtime/orphan').write_bytes(b'synthetic-orphan')
                    reset.atomic_json(root/'control/reset-pending.json',{'deployment':'vaultcontext-demo','generation':day,'phase':'pending'})
                    self.assertTrue(reset.reset(config,day)['reset'])
                    self.assertFalse((root/'runtime/orphan').exists())
                    self.assertTrue(store.preferences('survives')['salesContact'])
            finally:
                subprocess.run([sys.executable,str(script),'stop','--root',str(root)],check=True)
                http.shutdown();http.server_close();thread.join()


if __name__ == '__main__':
    unittest.main()
