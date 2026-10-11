"""An app release must explicitly declare the exact server/migration contract."""
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('compatibility',ROOT/'deploy/demo/verify_compatibility.py')
compatibility=importlib.util.module_from_spec(spec);spec.loader.exec_module(compatibility)


class CompatibilityTests(unittest.TestCase):
    def test_packaged_manifest_matches_exact_inputs(self):
        compatibility.verify(ROOT,ROOT/'deploy/demo/compatibility.json')

    def test_changed_migrations_require_new_reviewed_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'pb_migrations').mkdir()
            (root/'POCKETCONTEXT_VERSION').write_text('a'*40+'\n')
            migration=root/'pb_migrations/1_schema.js';migration.write_text('synthetic original')
            digest=hashlib.sha256()
            for relative in (Path('POCKETCONTEXT_VERSION'),Path('pb_migrations/1_schema.js')):
                digest.update(str(relative).encode()+b'\0'+(root/relative).read_bytes()+b'\0')
            policy=root/'compatibility.json'
            policy.write_text(json.dumps({'format':1,'schema':digest.hexdigest(),'acceptSchemas':[digest.hexdigest()],'acceptLegacyRevisions':[]}))
            compatibility.verify(root,policy)
            migration.write_text('synthetic changed migration')
            with self.assertRaises(ValueError):compatibility.verify(root,policy)

# Historical persisted schema at demo revision 194f724, before adopting the
# requester-admission server pin. Keep this fixture independent of the new
# declaration: removing support for the deployed predecessor must fail the test.
PREVIOUS_SCHEMA = '761af213fb927c7ef87bb0c09941a2b0fed7e7b567f3c31d109fcf75e4f8f370'


class PreviousSchemaUpgradeTests(unittest.TestCase):
    def exercise_upgrade(self, process=False):
        import test_demo_once_runtime as fixture
        policy = json.loads((ROOT/'deploy/demo/compatibility.json').read_text())
        old_policy = {'format': 1, 'schema': PREVIOUS_SCHEMA,
                      'acceptSchemas': [PREVIOUS_SCHEMA], 'acceptLegacyRevisions': []}
        module = fixture.runtime_module()
        day = datetime.now(timezone.utc).date().isoformat()
        with tempfile.TemporaryDirectory(prefix='previous-schema-upgrade-') as directory:
            root = Path(directory)/'storage'
            def backend():
                if process:
                    return fixture.ProcessBackend(root, os.environ['VAULTCONTEXT_TEST_BINARY'],
                                                  os.environ['VAULTCONTEXT_TEST_LITESTREAM'])
                return fixture.LocalBackend(root)
            old_backend = backend()
            old = module.Runtime(root, fixture.REVISION, old_backend, clock=lambda: day,
                                 compatibility=old_policy)
            candidate = None
            try:
                old.start();old.configure(fixture.fixture_bindings());old.initialize()
                if process:
                    document, ciphertext = old_backend.populate_real()
                else:
                    old_backend.populate()
                    before = old_backend.contents()
                baseline = old.baseline()
                old.close()
                self.assertEqual(module.private_json(root/'control/state.json')['schema'], PREVIOUS_SCHEMA)
                replacement = backend()
                candidate = module.Runtime(root, fixture.NEXT_REVISION, replacement,
                                           clock=lambda: day, compatibility=policy)
                candidate.start()
                self.assertTrue(candidate.ready)
                candidate.verify_baseline(baseline)
                self.assertEqual(module.private_json(root/'control/state.json')['schema'], policy['schema'])
                self.assertFalse((root/'control/upgrade-pending.json').exists())
                if process:
                    self.assertEqual(replacement.read_document(document)['metadata'], ciphertext)
                    preferences = replacement.contact_request('/preferences?subject=synthetic-once-account')
                    self.assertFalse(preferences['salesContact'] or preferences['newsletter'])
                else:
                    self.assertEqual(replacement.contents(), before)
            finally:
                if candidate is not None: candidate.close()
                old.close()

    def test_declared_predecessor_preserves_populated_databases(self):
        self.exercise_upgrade()

    @unittest.skipUnless(os.environ.get('VAULTCONTEXT_TEST_BINARY') and os.environ.get('VAULTCONTEXT_TEST_LITESTREAM'),
                         'requires pinned server and Litestream')
    def test_real_records_and_replicas_survive_declared_predecessor_upgrade(self):
        # Both starts use the candidate binary. This tests the real historical
        # durable-schema admission, preserved records and replication lifecycle;
        # it is not a cross-version binary or provider deployment test.
        self.exercise_upgrade(process=True)


if __name__=='__main__':unittest.main()
