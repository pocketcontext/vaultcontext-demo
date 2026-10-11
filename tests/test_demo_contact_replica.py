"""Isolated contact replica tests; optional real pinned Litestream with file storage."""
import importlib.util
import concurrent.futures
import sqlite3
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request,urlopen
from unittest.mock import patch, Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy/demo'))
import contact_replica as replica
import retention

class ContactReplicaTests(unittest.TestCase):
    def test_contact_replica_defaults_to_ten_seconds_and_honors_override(self):
        for override, expected in ((None, '10s'), ('2s', '2s')):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
                if override is not None:
                    os.environ['CONTACTS_LITESTREAM_SYNC_INTERVAL'] = override
                value = replica.Replica(Path(tmp)/'state/contacts.db', 'unused', 'unused', 'unused',
                                        local_replica=Path(tmp)/'remote')
                self.assertEqual(value.env['CONTACTS_LITESTREAM_SYNC_INTERVAL'], expected)
                self.assertIn('sync-interval: '+json.dumps(expected), Path(value.config).read_text())

    def test_credentials_are_separated(self):
        env=replica.child_environment({'CONTACTS_LITESTREAM_ACCESS_KEY_ID':'contact','CONTACTS_LITESTREAM_SECRET_ACCESS_KEY':'synthetic',
              'LITESTREAM_ACCESS_KEY_ID':'ephemeral','VAULTCONTEXT_S3_BUCKET':'ephemeral','AWS_SESSION_TOKEN':'synthetic'})
        self.assertEqual(env['LITESTREAM_ACCESS_KEY_ID'],'contact')
        self.assertNotIn('VAULTCONTEXT_S3_BUCKET',env);self.assertNotIn('AWS_SESSION_TOKEN',env)

    def test_fence_blocks_all_store_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'contacts.db';store=retention.Store(path)
            replica.atomic(Path(tmp)/'migration-fenced.json',{'fenced':True})
            for method,args in ((store.touch,('subject',)),(store.maintenance,()),(store.preferences,('subject',))):
                with self.assertRaises(retention.Rejected) as failure:method(*args)
                self.assertEqual(failure.exception.status,503)

    def test_source_fence_and_recovery_pending_precede_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'state';root.mkdir(mode=0o700)
            value=replica.Replica(root/'contacts.db','unused','unused',ROOT/'deploy/demo/retention.py',local_replica=Path(tmp)/'replica')
            for marker in ('migration-fenced.json','recovery.pending'):
                replica.atomic(root/marker,{'pending':True})
                with patch.object(value,'restore') as restore:
                    with self.assertRaises(replica.ReplicaError):value.prepare('adopt-handoff')
                    restore.assert_not_called()
                (root/marker).unlink()

    def test_fence_drains_an_external_operator_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'state';root.mkdir(mode=0o700)
            retention.Store(root/'contacts.db')
            db=sqlite3.connect(root/'contacts.db');db.execute('BEGIN IMMEDIATE')
            command=[sys.executable,str(ROOT/'deploy/demo/contact_replica.py'),'fence','--database',str(root/'contacts.db'),
                     '--service',str(ROOT/'deploy/demo/retention.py'),'--local-replica',str(Path(tmp)/'remote')]
            proc=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                time.sleep(.2)
                self.assertIsNone(proc.poll());self.assertFalse((root/'migration-fenced.json').exists())
                db.commit();db.close()
                self.assertEqual(proc.wait(timeout=5),0,proc.stderr.read().decode())
                self.assertTrue((root/'migration-fenced.json').is_file())
            finally:
                db.close()
                if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
                proc.stderr.close()

    def test_once_start_preserves_expired_rows_until_supervisor_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'state';root.mkdir(mode=0o700)
            value=replica.Replica(root/'contacts.db','unused','unused',ROOT/'deploy/demo/retention.py',local_replica=Path(tmp)/'replica')
            store=retention.Store(root/'contacts.db')
            store.enroll({'subject':'synthetic-expired','email':'synthetic@example.test','salesContact':False,'newsletter':False,
                          'termsVersion':'test','consentVersion':'test','expectedRevision':0},now=1)
            value.env['VAULTCONTEXT_DEMO_ONCE_CHILD']='contacts'
            value.startup_maintenance('start')
            self.assertEqual(store.preferences('synthetic-expired')['revision'],1)
            value.env.pop('VAULTCONTEXT_DEMO_ONCE_CHILD')
            value.startup_maintenance('start')
            self.assertEqual(store.preferences('synthetic-expired')['revision'],0)

    def test_retention_bound(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'CONTACTS_LITESTREAM_RETENTION_HOURS':'673'}):
            with self.assertRaises(replica.ReplicaError):
                replica.Replica(Path(tmp)/'state/contacts.db','unused','unused','unused',local_replica=Path(tmp)/'replica')

    def test_age_audit_rejects_old_missing_or_unreadable_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            value=replica.Replica(Path(tmp)/'state/contacts.db','unused','unused','unused',local_replica=Path(tmp)/'remote')
            now=1800000000
            def row(level,age):
                from datetime import datetime,timezone
                return {'level':level,'timestamp':datetime.fromtimestamp(now-age,timezone.utc).isoformat()}
            for rows,ok in [([row(9,0),row(0,28*86400)],True),([row(9,0),row(0,29*86400+1)],False),
                            ([row(9,301)],False),([row(0,0)],False),([],False),([row(9,-301)],False)]:
                result=Mock(returncode=0,stdout=json.dumps(rows).encode())
                with patch.object(replica.subprocess,'run',return_value=result):
                    if ok:self.assertEqual(value.audit_replica_age(now)['oldestListedAgeSeconds'],28*86400)
                    else:
                        with self.assertRaises(replica.ReplicaError):value.audit_replica_age(now)

    def test_refresh_orders_snapshot_restore_age_before_success_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            value=replica.Replica(Path(tmp)/'state/contacts.db','unused','unused','unused',local_replica=Path(tmp)/'remote')
            events=[]
            with patch.object(value,'command',side_effect=lambda *a,**kw:events.append('snapshot')), \
                 patch.object(value,'verify_remote',side_effect=lambda:events.append('restore')), \
                 patch.object(value,'audit_replica_age',side_effect=replica.ReplicaError('synthetic')):
                with self.assertRaises(replica.ReplicaError):value.refresh_replica()
            self.assertEqual(events,['snapshot','restore'])
            self.assertFalse((value.root/'replica-retention-check.json').exists())

class RealContactReplicaTests(unittest.TestCase):
    @unittest.skipUnless((os.environ.get('VAULTCONTEXT_DEMO_TEST_LITESTREAM') or os.environ.get('VAULTCONTEXT_TEST_LITESTREAM')),'set verified pinned Litestream binary')
    def test_populated_write_drain_handoff_restore_and_crash_recovery(self):
        with tempfile.TemporaryDirectory(prefix='contact-replica-test-') as tmp:
            base=Path(tmp);remote=base/'remote';source=base/'source';target=base/'target';crash=base/'crash'
            for path in (source,target,crash):path.mkdir(mode=0o700)
            token='synthetic-token-'+('x'*32)
            env={**os.environ,'VAULTCONTEXT_DEMO_CONTACT_TOKEN':token}
            script=ROOT/'deploy/demo/contact_replica.py';service=ROOT/'deploy/demo/retention.py'
            binary=os.environ.get('VAULTCONTEXT_DEMO_TEST_LITESTREAM') or os.environ['VAULTCONTEXT_TEST_LITESTREAM']
            with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            def command(mode,directory):
                return [sys.executable,str(script),mode,'--database',str(directory/'contacts.db'),'--litestream',binary,'--service',str(service),'--local-replica',str(remote),'--port',str(port)]
            def once(mode,directory):
                result=subprocess.run(command(mode,directory),env=env,capture_output=True,timeout=90)
                self.assertEqual(result.returncode,0,result.stderr.decode())
            def request(path,body=None):
                req=Request('http://127.0.0.1:'+str(port)+path,None if body is None else json.dumps(body).encode(),
                            {'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urlopen(req,timeout=3) as response:return json.load(response)
            def launch(directory):
                proc=subprocess.Popen(command('start',directory),env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    if proc.poll() is not None:self.fail('replica startup failed: '+proc.stderr.read().decode())
                    try:
                        if request('/health')['ready']:return proc
                    except (OSError,URLError):pass
                    time.sleep(.1)
                proc.terminate();proc.wait(timeout=60);self.fail('service not ready')
            once('init',source)
            # Refresh an idle unchanged snapshot; the scheduled snapshot path skips
            # unchanged TXIDs, but explicit -force-snapshot must rewrite it.
            value=replica.Replica(source/'contacts.db','unused',binary,service,local_replica=remote)
            old_snapshots=list(remote.glob('**/9/**/*.ltx'))
            self.assertTrue(old_snapshots)
            for snapshot in old_snapshots:os.utime(snapshot,(time.time()-31*86400,)*2)
            value.refresh_replica()
            self.assertTrue(all(time.time()-snapshot.stat().st_mtime<300 for snapshot in old_snapshots))
            self.assertTrue((source/'replica-retention-check.json').exists())
            proc=launch(source)
            try:
                body={'subject':'synthetic-google','email':'synthetic@example.test','salesContact':True,'newsletter':True,
                      'termsVersion':'demo-v1','consentVersion':'demo-v1','expectedRevision':0}
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    rows=list(pool.map(lambda index:request('/enroll',{**body,'subject':'parallel-'+str(index),'email':'parallel'+str(index)+'@example.test'}),range(8)))
                self.assertEqual(len(rows),8)
                consent=request('/enroll',body)
                request('/security',{'event':'enrollment','ip':'192.0.2.5','timestamp':int(time.time())})
                request('/unsubscribe',{'token':consent['unsubscribeToken']})
                request('/enroll',{**body,'subject':'active-google','email':'active@example.test'})
                once('fence',source)
                with self.assertRaises(HTTPError) as failure:request('/touch',{'subject':'active-google'})
                self.assertEqual(failure.exception.code,503);failure.exception.close()
                proc.terminate();self.assertEqual(proc.wait(timeout=90),0,proc.stderr.read().decode())
            finally:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=90)
                proc.stderr.close()
            manifest=json.loads((source/'contact-handoff.json').read_text())
            self.assertEqual(manifest['databaseDigest'],replica.validate_database(source/'contacts.db'))
            (target/'incoming-handoff.json').write_text(json.dumps(manifest));(target/'incoming-handoff.json').chmod(0o600)
            once('adopt-handoff',target)
            self.assertEqual(replica.validate_database(target/'contacts.db'),manifest['databaseDigest'])
            store=retention.Store(target/'contacts.db')
            self.assertTrue(store.preferences('active-google')['newsletter'])
            self.assertFalse(store.preferences('synthetic-google')['newsletter'])
            # Source is permanently fenced, including operator CLI/store paths.
            result=subprocess.run(command('start',source),env=env,capture_output=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            # An unverified disaster restore may preserve bytes, but not permission to market.
            proc=launch(crash)
            try:
                prefs=request('/preferences?subject=active-google')
                self.assertFalse(prefs['newsletter']);self.assertFalse(prefs['salesContact'])
                self.assertGreaterEqual(prefs['revision'],1<<51)
                with self.assertRaises(HTTPError) as stale:
                    request('/enroll',{**body,'subject':'active-google','email':'active@example.test','expectedRevision':2})
                self.assertEqual(stale.exception.code,409);stale.exception.close()
                self.assertFalse((crash/'recovery.pending').exists())
            finally:
                proc.terminate();self.assertEqual(proc.wait(timeout=90),0,proc.stderr.read().decode());proc.stderr.close()

if __name__=='__main__':unittest.main()
