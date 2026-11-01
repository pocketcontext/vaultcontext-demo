"""Demo paired SQLite recovery; isolated synthetic data, no provider access."""
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('demo_aux_runtime',ROOT/'docker/entrypoint.py')
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
TODAY=datetime.now(timezone.utc).date().isoformat()
ENV={'VAULTCONTEXT_DEMO_MODE':'true','VAULTCONTEXT_DEMO_GENERATION':TODAY,
     'LITESTREAM_BUCKET':'vaultcontext-demo-replicas','LITESTREAM_PATH':'demo/daily',
     'LITESTREAM_ACCESS_KEY_ID':'replica-key','LITESTREAM_SECRET_ACCESS_KEY':'replica-secret',
     'LITESTREAM_ENDPOINT':'https://synthetic.invalid','LITESTREAM_REGION':'auto',
     'VAULTCONTEXT_S3_BUCKET':'vaultcontext-demo-files','VAULTCONTEXT_S3_ACCESS_KEY_ID':'primary-key',
     'VAULTCONTEXT_S3_SECRET_ACCESS_KEY':'primary-secret','VAULTCONTEXT_S3_ENDPOINT':'https://synthetic.invalid',
     'VAULTCONTEXT_S3_REGION':'auto','VAULTCONTEXT_DEMO_CONTACT_URL':'http://127.0.0.1:8781',
     'VAULTCONTEXT_DEMO_CONTACT_TOKEN':'synthetic-token-'+'x'*32}

class DemoAuxiliaryTests(unittest.TestCase):
    def setUp(self):
        self.binary=os.environ.get('VAULTCONTEXT_TEST_BINARY',str(ROOT.parent/'pocketcontext/bin/pocketcontext'))
        self.litestream=os.environ.get('VAULTCONTEXT_TEST_LITESTREAM')
        self.tmp=tempfile.TemporaryDirectory(prefix='demo-aux-test-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir()
        self.source=self.root/'source';self.source.mkdir()
        self.config=self.root/'litestream.yml';self.config.write_text((ROOT/'docker/litestream.yml').read_text())
        self.env=patch.dict(os.environ,ENV,clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.constants=patch.multiple(runtime,DATA=self.data,CONFIG=str(self.config),DEMO_CONFIG=str(self.root/'demo.yml'))
        self.constants.start();self.addCleanup(self.constants.stop)

    def seed(self,folder,generation=TODAY):
        with closing(sqlite3.connect(folder/'data.db')) as db:
            db.executescript('CREATE TABLE demo_policy(id TEXT,enabled INTEGER,generation TEXT); CREATE TABLE synthetic(value TEXT);')
            db.execute('INSERT INTO demo_policy VALUES(?,?,?)',('demopolicy00001',1,generation))
            db.execute('INSERT INTO synthetic VALUES(?)',('main sentinel',));db.commit()
        with closing(sqlite3.connect(folder/'auxiliary.db')) as db:
            db.execute('CREATE TABLE _logs(value TEXT)');db.execute('INSERT INTO _logs VALUES(?)',('aux sentinel',));db.commit()

    def restore(self,args,**kwargs):
        name=Path(args[-1]).name
        shutil.copy2(self.source/name,Path(args[args.index('-o')+1]))

    def test_config_keeps_production_and_uses_separate_demo_prefix(self):
        before=self.config.read_bytes();runtime.configure_replication()
        generated=Path(runtime.DEMO_CONFIG).read_text()
        self.assertIn('l0-retention-check-interval: 5m', generated.splitlines())
        self.assertEqual(self.config.read_bytes(),before)
        self.assertIn('path: '+str(self.data/'auxiliary.db'),generated)
        self.assertIn('${LITESTREAM_DEMO_AUXILIARY_PATH}',generated)
        self.assertEqual(os.environ['LITESTREAM_DEMO_AUXILIARY_PATH'],'demo/daily/auxiliary')
        self.assertNotIn('replica-secret',generated);self.assertNotIn('primary-secret',generated)
        self.assertEqual(Path(runtime.DEMO_CONFIG).stat().st_mode & 0o777,0o600)
        with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_MODE':'false'}):
            self.assertEqual(runtime.replication_config(),str(self.config))
            with patch.object(Path,'read_text',side_effect=AssertionError('production rewritten')):runtime.configure_replication()

    def test_demo_prefix_rejects_ambiguous_paths(self):
        for path in ('','/absolute','a//b','a/../b','a/','bad"value','a\nb'):
            with self.subTest(path=path),patch.dict(os.environ,{'LITESTREAM_PATH':path}):
                with self.assertRaises(runtime.StartupError):runtime.configure_replication()

    def test_restore_installs_both_only_after_checks(self):
        self.seed(self.source)
        with patch.object(runtime,'run_command',side_effect=self.restore) as commands,patch.object(runtime,'verify_remote'):
            runtime.restore_database(self.data)
        self.assertEqual([Path(c.args[0][-1]).name for c in commands.call_args_list],['data.db','auxiliary.db'])
        with sqlite3.connect(self.data/'auxiliary.db') as db:self.assertEqual(db.execute('SELECT value FROM _logs').fetchone()[0],'aux sentinel')
        self.assertFalse((self.data/'restoration.pending').exists())

    def test_missing_aux_replica_installs_neither_database(self):
        self.seed(self.source)
        def restore(args,**kwargs):
            if args[-1].endswith('/auxiliary.db'):raise runtime.StartupError('missing auxiliary replica')
            self.restore(args)
        with patch.object(runtime,'run_command',side_effect=restore),patch.object(runtime,'verify_remote'):
            with self.assertRaises(runtime.StartupError):runtime.restore_database(self.data)
        self.assertEqual(list(self.data.iterdir()),[])

    def test_bad_auxiliary_and_expired_generation_never_install(self):
        self.seed(self.source,generation='2000-01-01')
        for bad_aux in (False,True):
            if bad_aux:(self.source/'auxiliary.db').write_bytes(b'corrupt')
            with patch.object(runtime,'run_command',side_effect=self.restore),patch.object(runtime,'verify_remote'):
                with self.assertRaises((runtime.StartupError,sqlite3.DatabaseError)):runtime.restore_database(self.data)
            self.assertEqual(list(self.data.iterdir()),[])

    def test_existing_main_cannot_recreate_missing_auxiliary(self):
        self.seed(self.data);(self.data/'auxiliary.db').unlink()
        with patch.object(runtime,'run_command') as command:
            with self.assertRaises(runtime.StartupError):runtime.restore_database(self.data)
        command.assert_not_called()
        (self.data/'auxiliary.db').touch()
        with self.assertRaises(runtime.StartupError):runtime.restore_database(self.data)

    def test_partial_pair_install_retains_durable_fence(self):
        self.seed(self.source);replace=os.replace
        def interrupted(source,target):
            if Path(target).name=='auxiliary.db':raise OSError('synthetic interruption')
            return replace(source,target)
        with patch.object(runtime,'run_command',side_effect=self.restore),patch.object(runtime,'verify_remote'),patch.object(runtime.os,'replace',side_effect=interrupted):
            with self.assertRaises(OSError):runtime.restore_database(self.data)
        self.assertTrue((self.data/'restoration.pending').is_file())
        with patch.object(runtime,'run_command') as command:
            with self.assertRaises(runtime.StartupError):runtime.restore_database(self.data)
        command.assert_not_called()

    def test_init_probes_both_replica_namespaces(self):
        with patch.object(runtime,'run_command') as command:runtime.restore_database(self.data,initialize=True)
        self.assertEqual(len(command.call_args_list),2)
        self.assertTrue(all('-if-replica-exists' in c.args[0] for c in command.call_args_list))
        self.seed(self.source)
        def only_aux(args,**kwargs):
            if args[-1].endswith('/auxiliary.db'):self.restore(args)
        with patch.object(runtime,'run_command',side_effect=only_aux):
            with self.assertRaises(runtime.StartupError):runtime.restore_database(self.data,initialize=True)
        self.assertEqual(list(self.data.iterdir()),[])

    def test_initial_sync_waits_for_both_and_failure_prevents_serve(self):
        self.seed(self.data)
        with patch.object(runtime,'run_command') as command,patch.object(runtime.os,'execve') as execute:runtime.serve()
        self.assertEqual([Path(c.args[0][-1]).name for c in command.call_args_list],['data.db','auxiliary.db'])
        self.assertEqual(execute.call_count,1)
        with patch.object(runtime,'run_command',side_effect=[None,runtime.StartupError('aux sync failed')]),patch.object(runtime.os,'execve') as execute:
            with self.assertRaises(runtime.StartupError):runtime.serve()
        execute.assert_not_called()

    def test_sync_cannot_extend_generation_past_midnight(self):
        self.seed(self.data)
        with patch.object(runtime,'run_command'),patch.object(runtime,'verify_demo_generation',side_effect=[None,runtime.StartupError('expired')]),patch.object(runtime.os,'execve') as execute:
            with self.assertRaises(runtime.StartupError):runtime.serve()
        execute.assert_not_called()

    def test_migration_fence_rejects_direct_source_restart(self):
        control=self.root/'control';control.mkdir()
        fence=control/'migration-fenced.json'
        runtime.write_private_json(fence,{'deployment':'vaultcontext-demo','generation':TODAY,'phase':'source-fenced'})
        with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_RESET_FENCE':str(control/'reset-pending.json'),'VAULTCONTEXT_DEMO_RESET_PHASE':'start'}):
            for mode in ('init','start','serve','verify','adopt-handoff'):
                with self.subTest(mode=mode),self.assertRaises(runtime.StartupError):runtime.demo_fence(mode)
            with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_RESET_PHASE':'handoff'}):runtime.demo_fence('handoff')
            runtime.write_private_json(fence,{'deployment':'vaultcontext-demo','generation':TODAY,'phase':'target-verified'})
            with self.assertRaises(runtime.StartupError):runtime.demo_fence('start')
            with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_RESET_PHASE':'migrate-start'}):
                runtime.demo_fence('start');runtime.demo_fence('serve')

    def test_handoff_modes_require_migration_fence_without_daily_reset(self):
        control=self.root/'control';control.mkdir()
        with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_RESET_FENCE':str(control/'reset-pending.json'),'VAULTCONTEXT_DEMO_RESET_PHASE':'handoff'}):
            with self.assertRaises(runtime.StartupError):runtime.demo_fence('handoff')
            runtime.write_private_json(control/'migration-fenced.json',{'deployment':'vaultcontext-demo','generation':TODAY,'phase':'source-fenced'})
            runtime.write_private_json(control/'reset-pending.json',{'deployment':'vaultcontext-demo','generation':TODAY,'phase':'initialized'})
            with self.assertRaises(runtime.StartupError):runtime.demo_fence('handoff')

    def test_digest_covers_domain_state_but_not_litestream_coordination(self):
        self.seed(self.data);before=runtime.database_digest(self.data/'data.db')
        with sqlite3.connect(self.data/'data.db') as db:
            db.execute('CREATE TABLE _litestream_seq(value INTEGER)');db.execute('INSERT INTO _litestream_seq VALUES(1)')
        self.assertEqual(before,runtime.database_digest(self.data/'data.db'))
        with sqlite3.connect(self.data/'data.db') as db:db.execute('UPDATE synthetic SET value=?',('changed',))
        self.assertNotEqual(before,runtime.database_digest(self.data/'data.db'))

    def test_handoff_refuses_a_source_that_changed_during_snapshot(self):
        self.seed(self.data)
        def command(args,**kwargs):
            if args[1]=='replicate':
                with sqlite3.connect(self.data/'data.db') as db:db.execute('UPDATE synthetic SET value=?',('changed',))
            else:shutil.copy2(self.data/Path(args[-1]).name,Path(args[args.index('-o')+1]))
        with patch.object(runtime,'run_command',side_effect=command),patch.object(runtime,'verify_remote'):
            with self.assertRaises(runtime.StartupError):runtime.handoff(self.data)
        self.assertFalse((self.root/'demo-handoff.json').exists())

    def test_adoption_compares_expected_manifest_before_publication(self):
        self.seed(self.source);control=self.root/'control';control.mkdir()
        manifest={'generation':TODAY,'databaseDigests':{name:'0'*64 for name in ('data.db','auxiliary.db')},'maintenance':None}
        runtime.write_private_json(control/'incoming-runtime-handoff.json',manifest)
        with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_RESET_FENCE':str(control/'reset-pending.json')}),patch.object(runtime,'run_command',side_effect=self.restore),patch.object(runtime,'verify_remote'):
            with self.assertRaises(runtime.StartupError):runtime.adopt_handoff(self.data)
        self.assertFalse((self.root/'demo-handoff.json').exists())

    def test_real_litestream_verified_handoff_and_adoption(self):
        if not self.litestream:self.skipTest('set VAULTCONTEXT_TEST_LITESTREAM to the pinned binary')
        self.seed(self.data);control=self.root/'control';control.mkdir()
        config=self.root/'file-replicas.json'
        def configuration(data):
            config.write_text(json.dumps({'dbs':[{'path':str(data/name),'replica':{'type':'file','path':str(self.root/'replicas'/name)}} for name in ('data.db','auxiliary.db')]}))
        configuration(self.data)
        with patch.object(runtime,'LITESTREAM',self.litestream),patch.object(runtime,'DEMO_CONFIG',str(config)),patch.object(runtime,'configure_replication'),patch.object(runtime,'verify_remote'):
            runtime.handoff(self.data)
            manifest=runtime.private_json(self.root/'demo-handoff.json')
            runtime.write_private_json(control/'incoming-runtime-handoff.json',manifest)
            target=self.root/'target';target.mkdir();configuration(target)
            with patch.dict(os.environ,{'VAULTCONTEXT_DEMO_RESET_FENCE':str(control/'reset-pending.json')}):runtime.adopt_handoff(target)
        for name,value in manifest['databaseDigests'].items():self.assertEqual(runtime.database_digest(target/name),value)

    def test_actual_server_demo_init_creates_both_before_serving(self):
        binary=Path(self.binary)
        if not binary.is_file():self.skipTest('pinned server binary unavailable; set VAULTCONTEXT_TEST_BINARY')
        real_command=runtime.run_command
        def command(args,**kwargs):
            if args[0]==runtime.LITESTREAM:
                self.assertIn('-if-replica-exists',args);return
            return real_command(args,**kwargs)
        with patch.object(runtime,'SERVER',str(binary)),patch.object(runtime,'APP',ROOT),patch.object(runtime,'run_command',side_effect=command),patch.object(runtime,'verify_remote'):
            previous=Path.cwd()
            try:
                os.chdir(ROOT);runtime.prepare(self.data,initialize=True)
            finally:os.chdir(previous)
        runtime.verify_auxiliary(self.data);runtime.verify_demo_generation(self.data)
        self.assertFalse((self.data/'initialization.pending').exists())

if __name__=='__main__':unittest.main()
