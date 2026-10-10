"""Standard stopped-container replacement without an external release authority."""
from unittest.mock import patch
import unittest
import test_demo_once_runtime as fixture


class StandardRuntimeTests(unittest.TestCase):
    setUp = fixture.OnceRuntimeStateTests.setUp
    close_all = fixture.OnceRuntimeStateTests.close_all
    populated = fixture.OnceRuntimeStateTests.populated

    def instance(self, revision=fixture.REVISION, backend=None):
        value, backend = fixture.OnceRuntimeStateTests.instance(self, revision, backend)
        value.compatibility = {'format': 1, 'schema': 'c' * 64, 'acceptSchemas': ['c' * 64], 'acceptLegacyRevisions': [fixture.REVISION]}
        return value, backend

    def test_standard_replacement_preserves_three_databases(self):
        old, backend = self.populated();before = backend.contents();old.close()
        candidate, processes = self.instance(fixture.NEXT_REVISION);candidate.start()
        self.assertTrue(candidate.ready)
        self.assertEqual(processes.contents(), before)
        self.assertFalse((self.root/'control/upgrade-pending.json').exists())

    def test_unrecognized_schema_cannot_start_writers(self):
        old, backend = self.populated();old.close()
        candidate, processes = self.instance(fixture.NEXT_REVISION)
        candidate.compatibility['acceptSchemas'] = ['d' * 64];candidate.start()
        self.assertEqual(candidate.phase, 'blocked')
        self.assertFalse(processes.app or processes.contacts or processes.opened)

    def test_interrupted_candidate_never_replays_migrations(self):
        old, backend = self.populated();old.close()
        class FailingBackend(fixture.LocalBackend):
            def start_app(self, *args, **kwargs):
                super().start_app(*args, **kwargs)
                raise RuntimeError('synthetic process interruption')
        failed, _ = self.instance(fixture.NEXT_REVISION, FailingBackend(self.root))
        with self.assertRaises(RuntimeError): failed.start()
        failed.close()
        retry, processes = self.instance(fixture.NEXT_REVISION);retry.start()
        self.assertEqual(retry.phase, 'blocked')
        self.assertFalse(processes.app or processes.contacts or processes.opened)

    def test_upgrade_crossing_midnight_resets_only_disposable_data(self):
        old, backend = self.populated();before = backend.contents();old.close()
        self.day[0] = '2026-10-10'
        candidate, processes = self.instance(fixture.NEXT_REVISION);candidate.start()
        self.assertTrue(candidate.ready)
        after = processes.contents()
        self.assertEqual(after['contacts'], before['contacts']);self.assertEqual(after['main'], [])
        self.assertFalse((self.root/'control/upgrade-pending.json').exists())

    def test_failure_persisting_validation_keeps_gate_closed(self):
        old, backend = self.populated();old.close()
        candidate, processes = self.instance(fixture.NEXT_REVISION)
        original = self.module.atomic
        def fail(path, value):
            if path.name == 'upgrade-pending.json' and value['phase'] == 'validated': raise OSError('synthetic fsync failure')
            return original(path, value)
        with patch.object(self.module, 'atomic', side_effect=fail), self.assertRaises(OSError): candidate.start()
        self.assertFalse(processes.opened or candidate.ready)

    def test_missing_database_never_initializes_or_purges(self):
        old, backend = self.populated();old.close()
        (self.root/'runtime/pb_data/auxiliary.db').unlink()
        candidate, processes = self.instance(fixture.NEXT_REVISION);candidate.start()
        self.assertEqual(candidate.phase, 'blocked');self.assertNotIn(('purge',), processes.events)

    def test_lost_state_never_resets_populated_volume(self):
        old, backend = self.populated();before=backend.contents();old.close()
        (self.root/'control/state.json').unlink()
        candidate, processes = self.instance(fixture.NEXT_REVISION);candidate.start()
        self.assertEqual(candidate.phase,'blocked');self.assertFalse(processes.opened)
        self.assertEqual(processes.contents(),before);self.assertNotIn(('purge',),processes.events)

    def test_corrupt_baseline_cannot_bypass_preservation(self):
        old, backend = self.populated();old.close()
        candidate, processes = self.instance(fixture.NEXT_REVISION)
        self.module.atomic(self.root/'control/upgrade-pending.json',{'format':1,'source':fixture.REVISION,'target':fixture.NEXT_REVISION,'schema':'c'*64,'phase':'prepared','baseline':[]})
        with self.assertRaises(self.module.RuntimeErrorSafe):candidate.start()
        self.assertFalse(processes.app or processes.contacts or processes.opened)

    def test_legacy_state_requires_explicit_revision_allowlist(self):
        old, backend = self.populated();old.close()
        state=self.module.private_json(self.root/'control/state.json');state.pop('schema')
        self.module.atomic(self.root/'control/state.json',state)
        candidate, processes = self.instance(fixture.NEXT_REVISION);candidate.compatibility['acceptLegacyRevisions']=[]
        candidate.start();self.assertFalse(candidate.ready);candidate.close()
        accepted, processes = self.instance(fixture.NEXT_REVISION);accepted.start();self.assertTrue(accepted.ready)

    def test_prepared_journal_is_recoverable_before_any_candidate_writer(self):
        old, backend = self.populated();baseline=old.baseline();before=backend.contents();old.close()
        candidate, processes = self.instance(fixture.NEXT_REVISION)
        self.module.atomic(self.root/'control/upgrade-pending.json',{'format':1,'source':fixture.REVISION,'target':fixture.NEXT_REVISION,'schema':'c'*64,'phase':'prepared','baseline':baseline})
        candidate.start();self.assertTrue(candidate.ready);self.assertEqual(processes.contents(),before)

    def test_validated_journal_recovers_after_failed_durable_removal(self):
        old, backend = self.populated();before=backend.contents();old.close()
        candidate, processes = self.instance(fixture.NEXT_REVISION)
        original=self.module.remove
        def fail(path):
            if path.name=='upgrade-pending.json':raise OSError('synthetic directory fsync failure')
            return original(path)
        with patch.object(self.module,'remove',side_effect=fail),self.assertRaises(OSError):candidate.start()
        self.assertFalse(candidate.ready or processes.opened);candidate.close()
        retry, processes=self.instance(fixture.NEXT_REVISION);retry.start()
        self.assertTrue(retry.ready);self.assertEqual(processes.contents(),before)

    def test_midnight_validated_journal_recovers_with_new_generation_baseline(self):
        old, backend = self.populated();contacts=backend.contents()['contacts'];old.close()
        self.day[0]='2026-10-10'
        candidate, processes=self.instance(fixture.NEXT_REVISION)
        original=self.module.remove
        def fail(path):
            if path.name=='upgrade-pending.json':raise OSError('synthetic marker failure')
            return original(path)
        with patch.object(self.module,'remove',side_effect=fail),self.assertRaises(OSError):candidate.start()
        self.assertFalse(candidate.ready);candidate.close()
        retry, processes=self.instance(fixture.NEXT_REVISION);retry.start()
        self.assertTrue(retry.ready);self.assertEqual(processes.contents()['contacts'],contacts)
        self.assertEqual(processes.contents()['main'],[])

    def test_termination_during_startup_never_opens_public_gate(self):
        old, backend=self.populated();old.close()
        candidate, processes=self.instance(fixture.NEXT_REVISION)
        start=processes.start_app
        def stop_during_start(*args,**kwargs):
            start(*args,**kwargs);candidate.stop_event.set()
        processes.start_app=stop_during_start
        with self.assertRaises(self.module.RuntimeErrorSafe):candidate.start()
        self.assertFalse(candidate.ready or processes.opened)

    def test_lost_configuration_cannot_be_rebound_through_environment(self):
        old, backend=self.populated();before=backend.contents();old.close()
        (self.root/'control/runtime.json').unlink()
        candidate, processes=self.instance(fixture.NEXT_REVISION)
        with patch.dict(fixture.os.environ,fixture.fixture_bindings()),self.assertRaises(self.module.RuntimeErrorSafe):candidate.start()
        self.assertFalse((self.root/'control/runtime.json').exists())
        self.assertFalse(processes.app or processes.contacts or processes.opened)
        self.assertEqual(processes.contents(),before)

    def test_fresh_initialization_does_not_purge_remote_recovery_data(self):
        old,backend=self.populated()
        self.assertNotIn(('purge',),backend.events)

    def test_populated_primary_blocks_fresh_initialization_before_any_writer(self):
        candidate, processes=self.instance();candidate.start();candidate.configure(fixture.fixture_bindings())
        def populated_primary():raise self.module.RuntimeErrorSafe('synthetic nonempty primary')
        processes.require_empty_primary=populated_primary
        with self.assertRaises(self.module.RuntimeErrorSafe):candidate.initialize()
        self.assertFalse(processes.app or processes.contacts or processes.opened)
        self.assertNotIn(('purge',),processes.events)

    def test_lost_all_databases_cannot_reinitialize_existing_state(self):
        old,backend=self.populated();old.close()
        state=(self.root/'control/state.json').read_bytes()
        for path in old.paths():path.unlink()
        candidate, processes=self.instance();candidate.start()
        with self.assertRaises(self.module.RuntimeErrorSafe):candidate.initialize()
        self.assertEqual((self.root/'control/state.json').read_bytes(),state)
        self.assertFalse(processes.app or processes.contacts or processes.opened)

class StandardProcessTests(unittest.TestCase):
    @unittest.skipUnless(fixture.os.environ.get('VAULTCONTEXT_TEST_BINARY') and fixture.os.environ.get('VAULTCONTEXT_TEST_LITESTREAM'), 'requires pinned server and Litestream')
    def test_populated_standard_replacement_and_noop_restart(self):
        day = fixture.datetime.now(fixture.timezone.utc).date().isoformat()
        with fixture.tempfile.TemporaryDirectory(prefix='standard-process-') as directory:
            root = fixture.Path(directory)/'storage'
            module = fixture.runtime_module()
            policy = {'format':1,'schema':'c'*64,'acceptSchemas':['c'*64],'acceptLegacyRevisions':[fixture.REVISION]}
            def create(revision):
                backend = fixture.ProcessBackend(root, fixture.os.environ['VAULTCONTEXT_TEST_BINARY'], fixture.os.environ['VAULTCONTEXT_TEST_LITESTREAM'])
                return module.Runtime(root,revision,backend,clock=lambda:day,compatibility=policy),backend
            old, backend = create(fixture.REVISION)
            old.compatibility=None  # Legacy durable state has no schema fingerprint.
            instances=[old]
            try:
                old.start();old.configure(fixture.fixture_bindings());old.initialize()
                self.assertNotIn('schema',module.private_json(root/'control/state.json'))
                document,ciphertext=backend.populate_real();old.close()
                for revision in (fixture.NEXT_REVISION,fixture.NEXT_REVISION):
                    candidate,processes=create(revision);instances.append(candidate);candidate.start()
                    self.assertTrue(candidate.ready)
                    self.assertEqual(processes.read_document(document)['metadata'],ciphertext)
                    consent=processes.contact_request('/preferences?subject=synthetic-once-account')
                    self.assertFalse(consent['salesContact'] or consent['newsletter'])
                    self.assertEqual(candidate.status()['databases'],3)
                    candidate.close()
            finally:
                for instance in reversed(instances): instance.close()

if __name__ == '__main__': unittest.main()
