import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('demo_entrypoint',Path(__file__).resolve().parents[1]/'docker/entrypoint.py')
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)


class DemoFenceTests(unittest.TestCase):
    def test_pending_fails_closed_and_explicit_reset_phases_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'reset-pending.json'
            marker.write_text(json.dumps({'deployment':'vaultcontext-demo','generation':'2026-10-09','phase':'pending'}));marker.chmod(0o600)
            env={'VAULTCONTEXT_DEMO_MODE':'true','VAULTCONTEXT_DEMO_GENERATION':'2026-10-09','VAULTCONTEXT_DEMO_RESET_FENCE':str(marker)}
            with patch.dict(os.environ,env,clear=True):
                for mode in ('start','init','serve','verify'):
                    with self.assertRaises(runtime.StartupError):runtime.demo_fence(mode)
                marker.write_text(json.dumps({'deployment':'vaultcontext-demo','generation':'2026-10-09','phase':'purged'}))
                with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_RESET_PHASE':'initialize'}):runtime.demo_fence('init')
                with self.assertRaises(runtime.StartupError):runtime.demo_fence('init')
                marker.unlink();runtime.demo_fence('start')

    def test_fence_must_live_outside_disposable_database(self):
        with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_MODE':'true','VAULTCONTEXT_DEMO_RESET_FENCE':str(runtime.DATA/'fence')},clear=True), self.assertRaises(runtime.StartupError):
            runtime.demo_fence('start')

class OnceEntrypointTests(unittest.TestCase):
    def test_once_child_uses_isolated_pair_and_loopback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'VAULTCONTEXT_DEMO_ONCE':'true','VAULTCONTEXT_DEMO_ONCE_CHILD':'app','VAULTCONTEXT_DEMO_ONCE_ROOT':tmp,'VAULTCONTEXT_DEMO_MODE':'true','LITESTREAM_PATH':'demo/data'},clear=True):
            child=importlib.util.module_from_spec(spec);spec.loader.exec_module(child)
            self.assertEqual(child.DATA,Path(tmp)/'runtime/pb_data')
            child.CONFIG=str(Path(__file__).resolve().parents[1]/'docker/litestream.yml')
            child.DEMO_CONFIG=str(Path(tmp)/'config.yml')
            child.configure_replication()
            generated=Path(child.DEMO_CONFIG).read_text()
            self.assertIn(str(child.DATA/'data.db'),generated)
            self.assertIn(str(child.DATA/'auxiliary.db'),generated)
            self.assertNotIn('/storage/pb_data/',generated)
            with patch.object(child,'validate_config'),patch.object(child,'verify_auxiliary'),patch.object(child,'verify_demo_generation'),patch.object(child,'run_command'),patch.object(child.os,'execve') as execute:
                child.serve()
            self.assertIn('--http=127.0.0.1:8081',execute.call_args.args[1])

    def test_once_bootstrap_does_not_require_private_bindings(self):
        with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_ONCE':'true'},clear=True),patch.object(runtime,'ONCE_CHILD',False),patch.object(runtime.os,'execv',side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):runtime.main([])
            self.assertEqual(execute.call_args.args,('/usr/local/bin/demo-supervisor',['/usr/local/bin/demo-supervisor']))
