"""Synthetic local session lifecycle regression tests (no server credentials)."""
import concurrent.futures
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vaultcontext_client import cli, crypto, session


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session.sock'
        self.cfg = {'url': 'https://synthetic.invalid', 'email': 'synthetic@example.com'}
        self.identity = crypto.generate_identity()
        self.path_patch = patch.object(cli, 'socket_path', return_value=self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def stale(self):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.path))
        os.chmod(self.path, 0o600)

    def test_real_fresh_process_and_stale_recovery(self):
        self.stale()
        session.start(self.cfg, self.identity, 'synthetic', 30)
        try:
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            for path in Path(self.tmp.name).iterdir():
                if path.is_file():
                    value = path.read_bytes()
                    for key in ('enc_private', 'sign_private'):
                        self.assertNotIn(self.identity[key].encode(), value)
        finally:
            self.assertEqual(session.stop(self.cfg), {'locked': True})
        self.assertFalse(self.path.exists())

    def test_stop_removes_stale_socket(self):
        self.stale()
        self.assertEqual(session.stop(self.cfg), {'locked': True})
        self.assertFalse(self.path.exists())

    def test_unsafe_paths_remain_untouched(self):
        self.path.write_text('synthetic non-socket')
        with self.assertRaises(cli.auth.Fail):
            session.stop(self.cfg)
        self.assertEqual(self.path.read_text(), 'synthetic non-socket')
        self.path.unlink()
        target = Path(self.tmp.name) / 'absent'
        self.path.symlink_to(target)
        with self.assertRaises(cli.auth.Fail):
            session.stop(self.cfg)
        self.assertTrue(self.path.is_symlink())

    def test_start_failure_cleans_created_socket(self):
        with patch.object(session.subprocess, 'Popen', side_effect=OSError('synthetic startup failure')):
            with self.assertRaises((OSError, cli.auth.Fail)):
                session.start(self.cfg, self.identity, 'synthetic', 30)
        self.assertFalse(self.path.exists())

    def test_old_worker_cannot_remove_replacement(self):
        self.stale()
        original = session.socket_info(self.path)
        old = self.path.with_name('old.sock')
        self.path.rename(old)
        self.stale()
        with self.assertRaises(cli.auth.Fail):
            session.unlink_owned(self.path, original)
        self.assertTrue(self.path.exists())

    def test_concurrent_unlocks_leave_one_usable_session(self):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(session.start, self.cfg, self.identity, 'synthetic', 30)
                           for _ in range(2)]
                for future in futures:
                    future.result(timeout=35)
            self.assertTrue(self.path.exists())
        finally:
            session.stop(self.cfg)
        self.assertFalse(self.path.exists())

    def test_worker_early_exit_and_readiness_timeout_clean_socket(self):
        popen = subprocess.Popen
        for child in ('raise SystemExit(1)', 'import time; time.sleep(5)'):
            with self.subTest(child=child):
                def launch(argv, **kwargs):
                    return popen([sys.executable, '-c', child], **kwargs)
                with patch.object(session.subprocess, 'Popen', side_effect=launch), \
                     patch.object(session, 'START_TIMEOUT', .2):
                    with self.assertRaises(cli.auth.Fail):
                        session.start(self.cfg, self.identity, 'synthetic', 30)
                self.assertFalse(self.path.exists())

    def test_killed_worker_can_be_replaced(self):
        popen = subprocess.Popen
        children = []
        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(session.subprocess, 'Popen', side_effect=launch):
            session.start(self.cfg, self.identity, 'synthetic', 30)
        children[0].kill()
        children[0].wait(timeout=5)
        self.assertTrue(self.path.exists())
        try:
            session.start(self.cfg, self.identity, 'synthetic', 30)
        finally:
            session.stop(self.cfg)
        self.assertFalse(self.path.exists())

    def test_logout_removes_stale_socket_and_cached_token(self):
        self.stale()
        token = Path(self.tmp.name) / 'token.json'
        token.write_text('synthetic saved token')
        legacy = Path(self.tmp.name) / 'legacy-token.json'
        legacy.write_text('synthetic cached token')
        with patch.object(cli.auth, 'session_file', return_value=token), \
             patch.object(cli.auth, 'legacy_session_file', return_value=legacy):
            self.assertEqual(session.stop(self.cfg, logout=True), {'signed_out': True})
        self.assertFalse(self.path.exists())
        self.assertFalse(token.exists())
        self.assertFalse(legacy.exists())

    def test_socket_disappears_during_close_wait(self):
        self.stale()
        original = session.socket_info
        calls = 0
        def inspect(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.unlink()
            return original(path)
        with patch.object(cli, 'session_call', return_value={'locked': True}), \
             patch.object(session, 'socket_info', side_effect=inspect):
            self.assertEqual(session.stop(self.cfg), {'locked': True})
        self.assertFalse(self.path.exists())

    def test_timeout_is_not_stale_evidence(self):
        self.stale()
        with patch.object(cli, 'session_call', side_effect=TimeoutError('synthetic timeout')):
            with self.assertRaises(cli.auth.Fail):
                session.stop(self.cfg)
        self.assertTrue(self.path.exists())
