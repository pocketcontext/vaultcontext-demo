"""An app release must explicitly declare the exact server/migration contract."""
import hashlib
import importlib.util
import json
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

if __name__=='__main__':unittest.main()
