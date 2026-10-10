"""Independent ONCE runtime state-machine and isolated process regression checks.

Fixtures are synthetic. No Docker socket, cloud storage, or deployed app is used.
"""
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REVISION = 'a' * 40
NEXT_REVISION = 'b' * 40
IMAGE = 'ghcr.io/pocketcontext/vaultcontext-demo@sha256:' + 'c' * 64


def fixture_bindings():
    """Only synthetic values; compatible with the dedicated storage separation."""
    values = {
        'VAULTCONTEXT_GOOGLE_CLIENT_ID': 'synthetic-client',
        'VAULTCONTEXT_GOOGLE_CLIENT_SECRET': 'synthetic-secret',
        'VAULTCONTEXT_DEMO_CONTACT_TOKEN': 'synthetic-contact-' + 'x' * 40,
    }
    for prefix, bucket in (
        ('VAULTCONTEXT_S3_', 'vaultcontext-demo-files-fixture'),
        ('LITESTREAM_', 'vaultcontext-demo-replica-fixture'),
        ('CONTACTS_LITESTREAM_', 'vaultcontext-demo-contacts-fixture'),
    ):
        values.update({prefix + 'BUCKET': bucket, prefix + 'ENDPOINT': 'https://synthetic.invalid',
                       prefix + 'REGION': 'auto', prefix + 'ACCESS_KEY_ID': prefix + 'synthetic',
                       prefix + 'SECRET_ACCESS_KEY': 'synthetic-secret'})
        if prefix != 'VAULTCONTEXT_S3_':
            values[prefix + 'PATH'] = 'synthetic/data'
    return values


class LocalBackend:
    """Real SQLite fixture behind the supervisor's process boundary.

    Event assertions examine observable state transitions, not implementation
    source. Separate process tests exercise the actual server and replication.
    """
    def __init__(self, root):
        self.root = Path(root)
        self.events = []
        self.app = False
        self.contacts = False
        self.opened = False
        self.fail_start = False

    def configure(self, bindings):
        self.bindings = dict(bindings)

    def start_app(self, generation, initialize=False):
        self.events.append(('app-start', generation, initialize))
        if self.fail_start:
            raise RuntimeError('synthetic candidate startup failure')
        directory = self.root / 'runtime/pb_data'
        directory.mkdir(parents=True, exist_ok=True)
        for name in ('data.db', 'auxiliary.db'):
            with closing(sqlite3.connect(directory / name)) as db:
                db.execute('CREATE TABLE IF NOT EXISTS synthetic_payload(id TEXT PRIMARY KEY, ciphertext BLOB)')
                if name == 'data.db':
                    db.execute('CREATE TABLE IF NOT EXISTS demo_policy(id TEXT PRIMARY KEY, enabled INTEGER, generation TEXT)')
                    db.execute('INSERT OR REPLACE INTO demo_policy VALUES (?,?,?)', ('demopolicy00001', 1, generation))
                db.commit()
        self.app = True

    def stop_app(self):
        self.events.append(('app-stop',))
        self.app = False

    def start_contacts(self, initialize=False):
        if self.contacts:
            raise RuntimeError('contact writer is already running')
        self.events.append(('contacts-start', initialize))
        directory = self.root / 'persistent'
        directory.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(directory / 'contacts.db')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS synthetic_contacts(id TEXT PRIMARY KEY, opted_in INTEGER)')
            db.commit()
        self.contacts = True

    def stop_contacts(self):
        self.events.append(('contacts-stop',))
        self.contacts = False

    def gate(self, opened):
        self.events.append(('gate', opened))
        self.opened = opened

    def healthy(self):
        return self.app and self.contacts

    def purge_ephemeral(self):
        self.events.append(('purge',))
        if self.app or self.opened:
            raise AssertionError('reset deletes while a writer or ingress is active')
        # This boundary purges remote replicas; Runtime owns local deletion.

    def maintain_contacts(self):
        self.events.append(('maintenance',))

    def close(self):
        self.gate(False)
        self.stop_app()
        self.stop_contacts()

    def populate(self):
        for name in ('data.db', 'auxiliary.db'):
            with closing(sqlite3.connect(self.root / 'runtime/pb_data' / name)) as db:
                db.execute('INSERT INTO synthetic_payload VALUES (?,?)', ('version-1', b'\x00\xffsynthetic-ciphertext'))
                db.commit()
        with closing(sqlite3.connect(self.root / 'persistent/contacts.db')) as db:
            db.execute('INSERT INTO synthetic_contacts VALUES (?,?)', ('withdrawn@example.test', 0))
            db.commit()

    def contents(self):
        result = {}
        for name, path, table in (
            ('main', self.root / 'runtime/pb_data/data.db', 'synthetic_payload'),
            ('auxiliary', self.root / 'runtime/pb_data/auxiliary.db', 'synthetic_payload'),
            ('contacts', self.root / 'persistent/contacts.db', 'synthetic_contacts'),
        ):
            with closing(sqlite3.connect(path)) as db:
                result[name] = db.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall()
        return result


class ProcessBackend(LocalBackend):
    """Pinned PocketContext + actual Litestream/contact subprocesses, local replicas.

    This adapter isolates process transport from the ONCE/container environment;
    it never substitutes test flags into shipped configuration or accesses S3.
    """
    def __init__(self, root, binary, litestream):
        super().__init__(root)
        import socket
        self.binary, self.litestream = binary, litestream
        self.fixture = self.root.parent / 'process-fixture'
        self.fixture.mkdir(exist_ok=True)
        self.remote = self.fixture / 'replicas'
        self.remote.mkdir(exist_ok=True)
        self._app_context = None
        self.replication = self.contact_process = None
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.contact_port = sock.getsockname()[1]
        self.contact_token = 'synthetic-contact-' + 'x' * 40

    def environment(self):
        values = {key: value for key, value in os.environ.items()
                  if not key.startswith(('VAULTCONTEXT_', 'LITESTREAM_', 'CONTACTS_', 'AWS_'))}
        values['VAULTCONTEXT_DEMO_CONTACT_TOKEN'] = self.contact_token
        return values

    def contact_request(self, path, body=None):
        from urllib.request import Request, urlopen
        request = Request('http://127.0.0.1:' + str(self.contact_port) + path,
                          None if body is None else json.dumps(body).encode(),
                          {'Authorization': 'Bearer ' + self.contact_token, 'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def start_contacts(self, initialize=False):
        import subprocess
        import sys
        import time
        self.events.append(('contacts-start', initialize))
        directory = self.root / 'persistent'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        command = [sys.executable, str(ROOT / 'deploy/demo/contact_replica.py'), 'start',
                   '--database', str(directory / 'contacts.db'), '--litestream', self.litestream,
                   '--service', str(ROOT / 'deploy/demo/retention.py'),
                   '--local-replica', str(self.remote / 'contacts'), '--port', str(self.contact_port)]
        if initialize:
            init_command = list(command)
            init_command[2] = 'init'
            result = subprocess.run(init_command, env=self.environment(), capture_output=True, timeout=90)
            if result.returncode:
                raise AssertionError('synthetic contact initialization failed: ' + result.stderr.decode())
        self.contact_process = subprocess.Popen(command, env=self.environment(),
                                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.contact_process.poll() is not None:
                raise AssertionError('synthetic contact process exited: ' + self.contact_process.stderr.read().decode())
            try:
                if self.contact_request('/health')['ready']:
                    self.contacts = True
                    return
            except OSError:
                pass
            time.sleep(.1)
        raise AssertionError('synthetic contact process timed out')

    def stop_contacts(self):
        if self.contact_process is not None:
            self.contact_process.terminate()
            result = self.contact_process.wait(timeout=90)
            output = self.contact_process.stderr.read().decode()
            self.contact_process.stderr.close()
            self.contact_process = None
            if result != 0:
                raise AssertionError('synthetic contact shutdown failed: ' + output)
        super().stop_contacts()

    def start_app(self, generation, initialize=False):
        import subprocess
        import sys
        from unittest.mock import patch
        if str(ROOT / 'tests') not in sys.path:
            sys.path.insert(0, str(ROOT / 'tests'))
        import integration
        self.events.append(('app-start', generation, initialize))
        values = self.environment()
        values.update(VAULTCONTEXT_DEMO_MODE='true', VAULTCONTEXT_DEMO_GENERATION=generation,
                      VAULTCONTEXT_DEMO_CONTACT_URL='http://127.0.0.1:' + str(self.contact_port),
                      BASE_URL='http://127.0.0.1:8769')
        data = self.root / 'runtime/pb_data'
        data.mkdir(parents=True, exist_ok=True)
        self._app_context = integration.server(self.binary, data_dir=data)
        with patch.dict(os.environ, values, clear=True):
            self.request = self._app_context.__enter__()
        self.app = True
        config = self.fixture / 'app-litestream.yml'
        content = 'socket:\n  enabled: true\n  path: ' + json.dumps(str(self.fixture / 'app.sock')) + '\ndbs:\n'
        for name in ('data.db', 'auxiliary.db'):
            content += ('  - path: ' + json.dumps(str(data / name)) + '\n    replica:\n      type: file\n      path: ' +
                        json.dumps(str(self.remote / name)) + '\n      sync-interval: 1s\n')
        config.write_text(content)
        self.replication = subprocess.Popen([self.litestream, 'replicate', '-config', str(config)],
                                            env=self.environment(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        import time
        deadline = time.monotonic() + 20
        socket_path = self.fixture / 'app.sock'
        while not socket_path.exists():
            if self.replication.poll() is not None or time.monotonic() > deadline:
                raise AssertionError('synthetic replica control socket unavailable')
            time.sleep(.05)
        for name in ('data.db', 'auxiliary.db'):
            result = subprocess.run([self.litestream, 'sync', '-wait', '-timeout', '20', '-socket',
                                     str(socket_path), str(data / name)], capture_output=True, timeout=25)
            if result.returncode:
                raise AssertionError('synthetic initial replica synchronization failed')

    def stop_app(self):
        if self._app_context is not None:
            self._app_context.__exit__(None, None, None)
            self._app_context = None
        if self.replication is not None:
            self.replication.terminate()
            result = self.replication.wait(timeout=90)
            output = self.replication.stderr.read().decode()
            self.replication.stderr.close()
            self.replication = None
            if result != 0:
                raise AssertionError('synthetic replica shutdown failed: ' + output)
        super().stop_app()

    def healthy(self):
        if not super().healthy():
            return False
        return (self.request('GET', '/api/demo/status')['enabled'] and
                self.contact_request('/health')['ready'] and self.replication.poll() is None)

    def populate_real(self):
        import base64
        from nacl.secret import SecretBox
        self.plaintext = b'\x00\xffisolated ONCE lifecycle synthetic sample'
        self.box = SecretBox(bytes(range(32)))
        self.ciphertext = base64.b64encode(self.box.encrypt(self.plaintext)).decode()
        admin = self.request('POST', '/api/collections/_superusers/auth-with-password',
                             {'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!'})['token']
        self.document = self.request('POST', '/api/collections/documents/records',
                                     {'metadata': self.ciphertext, 'revision': 1}, admin)['id']
        body = {'subject': 'synthetic-once-account', 'email': 'once@example.test',
                'salesContact': True, 'newsletter': True, 'termsVersion': 'demo-v1',
                'consentVersion': 'demo-v1', 'expectedRevision': 0}
        consent = self.contact_request('/enroll', body)
        self.contact_request('/unsubscribe', {'token': consent['unsubscribeToken']})
        self.contact_request('/security', {'event': 'enrollment', 'ip': '192.0.2.5', 'timestamp': int(__import__('time').time())})
        return self.document, self.ciphertext

    def read_document(self, document):
        admin = self.request('POST', '/api/collections/_superusers/auth-with-password',
                             {'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!'})['token']
        return self.request('GET', '/api/collections/documents/records/' + document, token=admin)


def runtime_module():
    spec = importlib.util.spec_from_file_location('independent_once_runtime', ROOT / 'deploy/demo/once_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OnceRuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.module = runtime_module()
        self.temporary = tempfile.TemporaryDirectory(prefix='once-runtime-regression-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'storage'
        self.day = ['2026-10-09']
        self.instances = []
        self.addCleanup(self.close_all)

    def test_release_identity_requires_dedicated_repository_digest(self):
        request = {'image': IMAGE, 'revision': REVISION}
        self.assertEqual(self.module.spec(request), request)
        for image in (
            IMAGE.replace('vaultcontext-demo@', 'vaultcontext@'),
            'ghcr.io/pocketcontext/vaultcontext-demo:latest',
            IMAGE.replace('pocketcontext/', 'foreign/'),
            IMAGE + 'extra',
        ):
            with self.subTest(image=image), self.assertRaises(self.module.RuntimeErrorSafe):
                self.module.spec({**request, 'image': image})

    def close_all(self):
        for value in reversed(self.instances):
            try:
                value.close()
            except Exception:
                pass

    def instance(self, revision=REVISION, backend=None):
        backend = backend or LocalBackend(self.root)
        value = self.module.Runtime(self.root, revision, backend, clock=lambda: self.day[0])
        self.instances.append(value)
        return value, backend

    def populated(self):
        value, backend = self.instance()
        value.start()
        value.configure(fixture_bindings())
        value.initialize()
        backend.populate()
        self.assertTrue(value.status()['ready'])
        self.assertTrue(backend.opened)
        return value, backend

    def test_empty_volume_requires_explicit_configuration_and_initialization(self):
        value, backend = self.instance()
        value.start()
        self.assertFalse(value.status()['ready'])
        self.assertFalse(value.status()['configured'])
        self.assertFalse(backend.opened)
        with self.assertRaises(Exception):
            value.initialize()
        self.assertFalse((self.root / 'runtime/pb_data/data.db').exists())
        value.configure(fixture_bindings())
        self.assertTrue(value.status()['configured'])
        self.assertFalse(value.status()['ready'])
        self.assertFalse(backend.opened)

    def test_overlapping_instance_cannot_start_writers_or_gate(self):
        old, backend = self.populated()
        before = backend.contents()
        second, candidate = self.instance(NEXT_REVISION)
        with self.assertRaises(Exception):
            second.start()
        self.assertFalse(candidate.app or candidate.contacts or candidate.opened)
        self.assertEqual(backend.contents(), before)
        self.assertTrue(old.status()['ready'])

    def test_restart_same_day_preserves_all_three_populated_databases(self):
        old, backend = self.populated()
        before = backend.contents()
        old.close()
        replacement, candidate = self.instance()
        replacement.start()
        self.assertTrue(replacement.status()['ready'])
        self.assertEqual(candidate.contents(), before)
        self.assertNotIn(('purge',), candidate.events)

    def test_midnight_clears_only_ephemeral_state(self):
        value, backend = self.populated()
        before = backend.contents()
        self.day[0] = '2026-10-10'
        value.tick()
        after = backend.contents()
        self.assertEqual(after['main'], [])
        self.assertEqual(after['auxiliary'], [])
        self.assertEqual(after['contacts'], before['contacts'])
        self.assertEqual(value.status()['generation'], self.day[0])
        self.assertTrue(value.status()['ready'])
        self.assertEqual(sum(event == ('purge',) for event in backend.events), 2)

    def test_release_requires_candidate_validation_before_public_commit(self):
        old, backend = self.populated()
        before = backend.contents()
        spec = {'image': IMAGE, 'revision': NEXT_REVISION}
        old.prepare_release(spec)
        self.assertFalse(backend.opened or backend.app or backend.contacts)
        old.close()
        candidate, processes = self.instance(NEXT_REVISION)
        candidate.start()
        self.assertFalse(processes.opened)
        self.assertEqual(processes.contents(), before)
        candidate.validate_release(spec)
        self.assertFalse(processes.opened)
        candidate.commit_release(spec)
        self.assertTrue(processes.opened)
        self.assertTrue(candidate.status()['ready'])
        self.assertEqual(processes.contents(), before)

    def test_wrong_revision_restart_stays_fenced_without_reset(self):
        old, backend = self.populated()
        before = backend.contents()
        old.prepare_release({'image': IMAGE, 'revision': NEXT_REVISION})
        old.close()
        wrong, processes = self.instance(REVISION)
        wrong.start()
        self.assertEqual(wrong.status()['phase'], 'blocked')
        self.assertFalse(processes.opened)
        self.assertNotIn(('purge',), processes.events)
        self.assertEqual(backend.contents(), before)

    def test_candidate_mutating_existing_data_never_opens_gate(self):
        old, backend = self.populated()
        old.prepare_release({'image': IMAGE, 'revision': NEXT_REVISION})
        old.close()
        class MutatingBackend(LocalBackend):
            def start_app(self, generation, initialize=False):
                super().start_app(generation, initialize)
                with closing(sqlite3.connect(self.root / 'runtime/pb_data/data.db')) as db:
                    db.execute("DELETE FROM synthetic_payload")
                    db.commit()
        candidate, processes = self.instance(NEXT_REVISION, MutatingBackend(self.root))
        with self.assertRaises(Exception):
            candidate.start()
        self.assertFalse(processes.opened)
        self.assertFalse(candidate.status()['ready'])
        self.assertFalse(processes.app or processes.contacts)

    def test_commit_marker_failure_closes_public_gate(self):
        from unittest.mock import patch
        old, backend = self.populated()
        spec = {'image': IMAGE, 'revision': NEXT_REVISION}
        old.prepare_release(spec)
        old.close()
        candidate, processes = self.instance(NEXT_REVISION)
        candidate.start()
        original = self.module.remove
        def fail_marker(path):
            if path.name == 'release-pending.json':
                raise OSError('synthetic durable marker failure')
            return original(path)
        with patch.object(self.module, 'remove', side_effect=fail_marker):
            with self.assertRaises(Exception):
                candidate.commit_release(spec)
        self.assertFalse(processes.opened)
        self.assertFalse(candidate.status()['ready'])

    def test_failed_child_drain_keeps_exclusive_writer_lease(self):
        class StuckBackend(LocalBackend):
            fail_close = True
            def close(self):
                if self.fail_close:
                    self.gate(False)
                    raise RuntimeError('synthetic writer still running')
                super().close()
        processes = StuckBackend(self.root)
        old, _ = self.instance(backend=processes)
        old.start()
        old.configure(fixture_bindings())
        old.initialize()
        with self.assertRaises(Exception):
            old.close()
        candidate, second = self.instance(NEXT_REVISION)
        try:
            with self.assertRaises(Exception):
                candidate.start()
            self.assertFalse(second.app or second.contacts or second.opened)
        finally:
            processes.fail_close = False
            old.close()

    def test_process_exit_after_prepare_keeps_durable_fence_and_data(self):
        import subprocess
        import sys
        program = r"""
import importlib.util, os, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('fixture',sys.argv[1]);fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
root=Path(sys.argv[2]);module=fixture.runtime_module();backend=fixture.LocalBackend(root)
runtime=module.Runtime(root,fixture.REVISION,backend,clock=lambda:'2026-10-09')
runtime.start();runtime.configure(fixture.fixture_bindings());runtime.initialize();backend.populate()
runtime.prepare_release({'image':fixture.IMAGE,'revision':fixture.NEXT_REVISION})
os._exit(23)
"""
        result = subprocess.run([sys.executable, '-c', program, str(Path(__file__)), str(self.root)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        self.assertTrue((self.root/'control/release-pending.json').exists())
        candidate, processes = self.instance(NEXT_REVISION)
        candidate.start()
        self.assertFalse(processes.opened)
        state = processes.contents()
        self.assertEqual(state['main'], [('version-1', b'\x00\xffsynthetic-ciphertext')])
        self.assertEqual(state['contacts'], [('withdrawn@example.test', 0)])
        candidate.commit_release({'image': IMAGE, 'revision': NEXT_REVISION})
        self.assertTrue(candidate.status()['ready'])

    def test_abort_after_lost_commit_ack_fences_exact_release(self):
        old, backend = self.populated()
        before = backend.contents()
        spec = {'image': IMAGE, 'revision': NEXT_REVISION}
        old.prepare_release(spec)
        old.close()
        candidate, processes = self.instance(NEXT_REVISION)
        candidate.start()
        candidate.commit_release(spec)
        with self.assertRaises(Exception):
            candidate.abort_release({**spec, 'revision': 'f' * 40})
        self.assertTrue(candidate.status()['ready'])
        candidate.abort_release(spec)
        self.assertFalse(candidate.status()['ready'])
        self.assertFalse(processes.opened or processes.app or processes.contacts)
        self.assertEqual(processes.contents(), before)
        candidate.close()
        restarted, again = self.instance(NEXT_REVISION)
        restarted.start()
        self.assertEqual(restarted.status()['phase'], 'blocked')
        self.assertFalse(again.opened or again.app or again.contacts)

    def test_unprepared_revision_change_cannot_start_existing_volume(self):
        old, backend = self.populated()
        before = backend.contents()
        old.close()
        candidate, processes = self.instance(NEXT_REVISION)
        candidate.start()
        self.assertEqual(candidate.status()['phase'], 'blocked')
        self.assertFalse(processes.app or processes.contacts or processes.opened)
        self.assertEqual(processes.contents(), before)

    def test_adoption_checks_preservation_after_starting_candidate(self):
        import hashlib
        import shutil
        source, backend = self.populated()
        before = backend.contents()
        source.close()
        source_root = self.root
        self.root = Path(self.temporary.name) / 'adopted'
        class MutatingBackend(LocalBackend):
            def start_contacts(self, initialize=False):
                super().start_contacts(initialize)
                with closing(sqlite3.connect(self.root / 'persistent/contacts.db')) as db:
                    db.execute('UPDATE synthetic_contacts SET opted_in=1')
                    db.commit()
        target, processes = self.instance(backend=MutatingBackend(self.root))
        target.start()
        target.configure(fixture_bindings())
        paths = {'data.db': source_root/'runtime/pb_data/data.db',
                 'auxiliary.db': source_root/'runtime/pb_data/auxiliary.db',
                 'contacts.db': source_root/'persistent/contacts.db'}
        files = {name: {'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                 for name, path in paths.items()}
        target.adopt_begin({'generation': self.day[0], 'image': IMAGE, 'revision': REVISION, 'files': files})
        for name, path in paths.items():
            shutil.copyfile(path, self.root/'runtime/adoption'/name)
        with self.assertRaises(Exception):
            target.adopt_finish()
        self.assertFalse(target.status()['ready'])
        self.assertFalse(processes.app or processes.contacts or processes.opened)
        self.assertTrue((self.root/'control/adoption-pending.json').exists())
        self.assertEqual(backend.contents(), before)

    def test_daily_contact_maintenance_runs_once_after_cutoff_and_survives_restart(self):
        value, backend = self.populated()
        value.minute = lambda: 4
        value.tick()
        self.assertNotIn(('maintenance',), backend.events)
        value.minute = lambda: 6
        value.tick()
        value.tick()
        self.assertEqual(backend.events.count(('maintenance',)), 1)
        value.close()
        restarted, replacement = self.instance()
        restarted.minute = lambda: 6
        restarted.start()
        restarted.tick()
        self.assertNotIn(('maintenance',), replacement.events)
        self.day[0] = '2026-10-10'
        restarted.tick()
        restarted.tick()
        self.assertEqual(replacement.events.count(('maintenance',)), 1)

    def test_midnight_during_prepared_release_never_reopens_old_generation(self):
        old, backend = self.populated()
        before = backend.contents()
        spec = {'image': IMAGE, 'revision': NEXT_REVISION}
        old.prepare_release(spec)
        old.close()
        self.day[0] = '2026-10-10'
        candidate, processes = self.instance(NEXT_REVISION)
        candidate.start()
        self.assertEqual(candidate.status()['phase'], 'blocked')
        self.assertFalse(processes.opened)
        self.assertNotIn(('purge',), processes.events)
        self.assertEqual(backend.contents(), before)


class OnceRuntimeProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('VAULTCONTEXT_TEST_BINARY') and os.environ.get('VAULTCONTEXT_TEST_LITESTREAM'),
                         'set verified pinned server and Litestream paths for process fixture')
    def test_populated_three_database_release_with_real_server_and_local_replicas(self):
        import base64
        import subprocess
        module = runtime_module()
        day = datetime.now(timezone.utc).date().isoformat()
        with tempfile.TemporaryDirectory(prefix='once-process-regression-') as temporary:
            root = Path(temporary) / 'storage'
            backend = ProcessBackend(root, os.environ['VAULTCONTEXT_TEST_BINARY'], os.environ['VAULTCONTEXT_TEST_LITESTREAM'])
            old = module.Runtime(root, REVISION, backend, clock=lambda: day)
            candidate = None
            try:
                old.start()
                old.configure(fixture_bindings())
                old.initialize()
                document, ciphertext = backend.populate_real()
                plaintext = backend.box.decrypt(base64.b64decode(ciphertext))
                self.assertEqual(plaintext, backend.plaintext)
                spec = {'image': IMAGE, 'revision': NEXT_REVISION}
                old.prepare_release(spec)
                self.assertFalse(backend.opened or backend.app or backend.contacts)
                # All three real replicas must be independently recoverable after drain.
                for name, live in [('data.db', root/'runtime/pb_data/data.db'),
                                   ('auxiliary.db', root/'runtime/pb_data/auxiliary.db'),
                                   ('contacts.db', root/'persistent/contacts.db')]:
                    restored = Path(temporary) / ('restored-' + name)
                    result = subprocess.run([backend.litestream, 'restore', '-o', str(restored), '-integrity-check', 'full',
                                             'file://' + str(backend.remote / name if name != 'contacts.db' else backend.remote / 'contacts')],
                                            capture_output=True, timeout=90)
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    self.assertEqual(module.database_snapshot(live), module.database_snapshot(restored))
                old.close()
                replacement = ProcessBackend(root, backend.binary, backend.litestream)
                candidate = module.Runtime(root, NEXT_REVISION, replacement, clock=lambda: day)
                candidate.start()
                self.assertFalse(replacement.opened)
                candidate.validate_release(spec)
                candidate.commit_release(spec)
                self.assertTrue(candidate.status()['ready'])
                self.assertEqual(replacement.read_document(document)['metadata'], ciphertext)
                self.assertEqual(backend.box.decrypt(base64.b64decode(ciphertext)), plaintext)
                preferences = replacement.contact_request('/preferences?subject=synthetic-once-account')
                self.assertFalse(preferences['salesContact'] or preferences['newsletter'])
                self.assertEqual(candidate.status()['databases'], 3)
            finally:
                if candidate is not None:
                    candidate.close()
                old.close()


if __name__ == '__main__':
    unittest.main()
