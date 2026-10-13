"""Synthetic private contact export and credential boundary checks."""
import hashlib
import io
from contextlib import redirect_stderr
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_retention', ROOT/'deploy/demo/retention.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = module.Store(self.root/'contacts.db')
        self.token = 'synthetic-export-credential-' + 'x'*32
        self.internal = 'synthetic-internal-credential-' + 'y'*32
        self.credentials = self.root/'export.json'
        self.entry = {'id': 'test', 'sha256': hashlib.sha256(self.token.encode()).hexdigest(), 'enabled': True}
        self.write_credentials()
        self.server = module.ThreadingHTTPServer(('127.0.0.1', 0), module.handler(self.store, self.internal, str(self.credentials)))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def write_credentials(self):
        self.credentials.write_text(json.dumps({'version': 1, 'credentials': [self.entry]}))
        self.credentials.chmod(0o600)

    def request(self, path='/export/v1', token=None, body=None):
        headers = {'Authorization': 'Bearer ' + (token or self.token)}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.base + path, headers=headers, data=None if body is None else json.dumps(body).encode())
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def seed(self):
        return self.store.enroll({'subject': 'synthetic-person', 'email': 'person@example.test', 'salesContact': True,
            'newsletter': True, 'termsVersion': 't1', 'consentVersion': 'c1', 'expectedRevision': 0})

    def test_auth_scope_revocation_expiry_and_unsafe_config(self):
        self.assertEqual(self.request()[0], 200)
        for token in (self.internal, 'visitor-token', 'z'*64):
            self.assertEqual(self.request(token=token)[0], 401)
        self.assertEqual(self.request('/health')[0], 401)
        self.assertEqual(self.request('/maintenance', body={})[0], 401)
        self.assertEqual(self.request(body={})[0], 404)
        self.assertEqual(self.request('/export/v1?anything=1')[0], 404)
        self.entry['enabled'] = False
        self.write_credentials()
        self.assertEqual(self.request()[0], 401)
        self.entry['enabled'] = True
        self.entry['expires_at'] = int(time.time())-1
        self.write_credentials()
        self.assertEqual(self.request()[0], 401)
        self.entry['expires_at'] += 3600
        self.write_credentials()
        self.assertEqual(self.request()[0], 200)
        self.credentials.chmod(0o644)
        self.assertEqual(self.request()[0], 401)
        self.credentials.unlink()
        self.assertEqual(self.request()[0], 401)

    def test_default_private_path_needs_no_runtime_reconfiguration(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = module.ThreadingHTTPServer(('127.0.0.1', 0), module.handler(self.store, self.internal))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.assertEqual(self.request()[0], 401)
        self.credentials.rename(self.root/'export-credentials.json')
        self.assertEqual(self.request()[0], 200)

    def test_runtime_export_path_reaches_only_contact_child(self):
        import test_demo_once_runtime as fixture
        runtime = fixture.runtime_module()
        key = 'VAULTCONTEXT_DEMO_EXPORT_CREDENTIALS_FILE'
        values = fixture.fixture_bindings()
        values[key] = str(self.credentials)
        self.assertEqual(runtime.validate_bindings(values)[key], str(self.credentials))
        backend = runtime.Backend(self.root, 'https://demo.example.test')
        backend.configure(values)
        self.assertNotIn(key, backend.app_env('2026-10-13'))
        from contextlib import ExitStack
        with ExitStack() as stack:
            launch = stack.enter_context(patch.object(backend, 'launch'))
            if hasattr(backend, 'prepare_storage'):
                stack.enter_context(patch.object(backend, 'prepare_storage'))
            backend.start_contacts()
        self.assertEqual(launch.call_args.args[2][key], str(self.credentials))

    def test_fifo_credentials_fail_without_blocking(self):
        self.credentials.unlink()
        os.mkfifo(self.credentials, 0o600)
        self.assertEqual(self.request()[0], 401)

    def test_export_audit_contains_only_attribution_and_counts(self):
        self.seed()
        output = io.StringIO()
        audited = threading.Event()
        original_audit = module.export_audit
        def audit(*args):
            original_audit(*args)
            if args[1] == 401:
                audited.set()
        with redirect_stderr(output), patch.object(module, 'export_audit', side_effect=audit):
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.request(token=self.internal)[0], 401)
            self.assertTrue(audited.wait(2))
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['credentialId'], 'test')
        self.assertEqual(events[0]['outcome'], 200)
        self.assertEqual(events[0]['counts']['contacts'], 1)
        self.assertIsNone(events[1]['credentialId'])
        self.assertEqual(events[1]['outcome'], 401)
        for event in events:
            self.assertEqual(set(event), {'event', 'credentialId', 'source', 'snapshotId', 'counts', 'outcome'})
        for forbidden in (self.token, self.internal, 'person@example.test', 'synthetic-person'):
            self.assertNotIn(forbidden, output.getvalue())

    def test_complete_business_snapshot_excludes_capabilities(self):
        enrollment = self.seed()
        self.store.review({'subject':'synthetic-person','expectedRevision':1,'decision':'retain','reason':'Synthetic review'})
        self.store.unsubscribe(enrollment['unsubscribeToken'])
        self.store.security({'event': 'auth_success', 'ip': '192.0.2.1', 'timestamp': int(time.time())})
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(result['complete'])
        self.assertEqual(result['schemaVersion'], 1)
        self.assertEqual(result['source'], ROOT.name)
        self.assertEqual(set(result['datasets']), set(module.EXPORT_FIELDS))
        for table, records in result['datasets'].items():
            self.assertEqual(result['counts'][table], len(records))
            for record in records:
                self.assertEqual(set(record), set(module.EXPORT_FIELDS[table]))
        raw = json.dumps(result)
        for excluded in ('token_hash', 'unsubscribe_tokens', 'security_events', enrollment['unsubscribeToken'], '192.0.2.1'):
            self.assertNotIn(excluded, raw)
        self.assertEqual(result['counts']['consent_events'], 2)
        self.assertEqual(result['counts']['suppression'], 1)
        self.assertEqual(result['counts']['review_events'], 1)
        with self.store.connect() as db:
            for table, fields in module.EXPORT_FIELDS.items():
                expected = [dict(row) for row in db.execute('SELECT ' + ','.join(fields) + ' FROM ' + table + ' ORDER BY ' + fields[0])]
                self.assertEqual(result['datasets'][table], expected)

    def test_bounds_and_schema_drift_fail_without_partial_data(self):
        self.seed()
        with patch.object(module, 'EXPORT_MAX_ROWS', 1):
            status, result = self.request()
        self.assertEqual(status, 413)
        self.assertNotIn('datasets', result)
        with patch.object(module, 'EXPORT_MAX_BYTES', 4096):
            self.assertEqual(self.request()[0], 413)
        fence = self.root/'migration-fenced.json'
        fence.write_text('{}')
        self.assertEqual(self.request()[0], 503)
        fence.unlink()
        with self.store.connect() as db:
            db.execute('ALTER TABLE contacts ADD COLUMN future_field TEXT')
        self.assertEqual(self.request()[0], 503)

    def test_snapshot_stays_consistent_during_concurrent_write(self):
        self.seed()
        original_connect = module.sqlite3.connect
        store = self.store
        done = False
        class Connection:
            def __init__(self, connection):
                self.connection = connection
            def __setattr__(self, key, value):
                if key == 'row_factory':
                    self.connection.row_factory = value
                else:
                    object.__setattr__(self, key, value)
            def execute(self, sql, *args):
                nonlocal done
                if sql.startswith('SELECT id,subject,at,sales') and not done:
                    done = True
                    store.enroll({'subject':'synthetic-person','email':'person@example.test','salesContact':False,
                        'newsletter':False,'termsVersion':'t1','consentVersion':'c1','expectedRevision':1})
                return self.connection.execute(sql, *args)
            def rollback(self): self.connection.rollback()
            def close(self): self.connection.close()
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            return Connection(connection) if kwargs.get('uri') else connection
        with patch.object(module.sqlite3, 'connect', side_effect=connect), patch.object(module.time, 'time', side_effect=[100, 200]):
            result = self.store.export_snapshot()
        self.assertEqual(result['generatedAt'], 100)
        self.assertTrue(done)
        self.assertEqual(result['datasets']['contacts'][0]['revision'], 1)
        self.assertEqual(result['counts']['consent_events'], 1)
        self.assertEqual(result['counts']['suppression'], 0)
        self.assertEqual(self.store.preferences('synthetic-person')['revision'], 2)


if __name__ == '__main__':
    unittest.main()
