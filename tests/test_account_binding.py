"""Account-bound memory workers, with synthetic Google sessions only."""
import base64
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from vaultcontext_client import auth, cli, crypto, demo, session


class AccountBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'VAULTCONTEXT_SESSION_DIR': self.tmp.name, 'XDG_CACHE_HOME': self.tmp.name + '/cache'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.cfg = {'url': 'https://synthetic.invalid', 'email': 'synthetic@example.com',
                    '_expected_account': 'original0000001'}
        self.cache('original0000001')

    def cache(self, account, fresh=True):
        claims = base64.urlsafe_b64encode(json.dumps({'exp': time.time()+3600}).encode()).decode().rstrip('=')
        value = dict(url=self.cfg['url'], email=self.cfg['email'], method='google',
                     token='synthetic.'+claims+'.signature', refreshed_at=time.time() if fresh else 0)
        if account: value['account'] = account
        auth.save_session(self.cfg, value)

    def response(self, account):
        return {'token': 'synthetic-refreshed', 'record': {'id': account, 'email': self.cfg['email'], 'collectionName': 'users'}}

    def test_replaced_cache_rejected_before_any_request(self):
        self.cache('replacement0001')
        with patch.object(auth, 'send') as send, patch.object(auth, 'google_login') as login:
            with self.assertRaisesRegex(auth.Fail, 'changed the unlocked account'):
                auth.call(self.cfg, 'POST', '/api/collections/vault_actions/records', {})
        send.assert_not_called(); login.assert_not_called()

    def test_renewal_cannot_change_account_or_overwrite_cache(self):
        self.cache('original0000001', fresh=False)
        with patch.object(auth, 'oauth_send', return_value=(200, self.response('replacement0001'))), patch.object(auth, 'send') as send:
            with self.assertRaisesRegex(auth.Fail, 'changed the unlocked account'):
                auth.call(self.cfg, 'POST', '/api/context/query', {})
        send.assert_not_called()
        self.assertEqual(auth.load_session(self.cfg)['account'], 'original0000001')

    def test_same_account_renewal_sends_new_token_and_keeps_binding(self):
        self.cache('original0000001', fresh=False)
        with patch.object(auth, 'oauth_send', return_value=(200, self.response('original0000001'))), patch.object(auth, 'send', return_value=(200, {})) as send:
            auth.call(self.cfg, 'POST', '/api/context/query', {})
        self.assertEqual(send.call_args.args[-1], 'synthetic-refreshed')
        self.assertEqual(auth.load_session(self.cfg)['account'], 'original0000001')
        self.assertEqual(self.cfg['_expected_account'], 'original0000001')

    def test_missing_login_and_rejected_refresh_never_sign_in(self):
        for missing in (False, True):
            if missing: auth.session_file(self.cfg).unlink()
            else: self.cache('original0000001', fresh=False)
            with patch.object(auth, 'oauth_send', return_value=(401, {})), patch.object(auth, 'google_login') as login, patch.object(auth, 'send') as send:
                with self.assertRaises(auth.Fail): auth.call(self.cfg, 'POST', '/api/context/query', {})
                login.assert_not_called(); send.assert_not_called()

    def test_idless_cache_verified_before_use(self):
        for account in ('original0000001', 'replacement0001'):
            self.cache(None)
            with patch.object(auth, 'oauth_send', return_value=(200, self.response(account))) as refresh, patch.object(auth, 'send', return_value=(200, {})) as send:
                if account == 'original0000001':
                    self.assertEqual(auth.call(self.cfg, 'POST', '/api/context/query', {}), (200, {}))
                    send.assert_called_once()
                else:
                    with self.assertRaises(auth.Fail): auth.call(self.cfg, 'POST', '/api/context/query', {})
                    send.assert_not_called()
                refresh.assert_called_once()

    def test_execute_binds_without_mutating_configuration(self):
        cfg = {'url': self.cfg['url'], 'email': self.cfg['email']}
        with patch.object(cli, 'rows', return_value=[]) as rows:
            self.assertEqual(cli.execute(cfg, {}, 'original0000001', {'command': 'vaults'}), [])
        self.assertEqual(rows.call_args.args[0]['_expected_account'], 'original0000001')
        self.assertNotIn('_expected_account', cfg)

    def test_real_worker_rejects_replaced_login_and_reset(self):
        requests = []
        state = {'generation': '2099-01-01'}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'enabled': True, 'generation': state['generation'], 'resetAt': state['generation'][:8] + ('02' if state['generation'].endswith('01') else '03') + 'T00:00:00Z'}).encode())
            def do_POST(self):
                requests.append(self.path)
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"rows":[],"columns":[]}')
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.cfg.update(url=f'http://127.0.0.1:{server.server_port}', _demo_generation='2099-01-01')
        self.cfg.pop('_expected_account')
        self.cache('original0000001')
        path = Path(self.tmp.name)/'worker.sock'
        with patch.object(cli, 'socket_path', return_value=path):
            session.start(self.cfg, crypto.generate_identity(), 'original0000001', 30)
            try:
                self.assertEqual(cli.session_call(self.cfg, {'command': 'vaults'}), [])
                requests.clear(); self.cache('replacement0001')
                with self.assertRaisesRegex(auth.Fail, 'changed the unlocked account'):
                    cli.session_call(self.cfg, {'command': 'vaults'})
                self.assertEqual(requests, [])
                auth.session_file(self.cfg).unlink()
                with self.assertRaisesRegex(auth.Fail, 'Run vaultcontext login'):
                    cli.session_call(self.cfg, {'command': 'vaults'})
                self.assertEqual(requests, [])
                self.cache('original0000001'); state['generation'] = '2099-01-02'
                with self.assertRaisesRegex(auth.Fail, 'demo reset'):
                    cli.session_call(self.cfg, {'command': 'vaults'})
                self.assertEqual(requests, [])
            finally: session.stop(self.cfg)

    def test_worker_activity_does_not_extend_deadline(self):
        conn = Mock(); conn.__enter__ = Mock(return_value=conn); conn.__exit__ = Mock(return_value=False)
        server = Mock(); server.accept.return_value = (conn, None)
        with patch.object(session.time, 'monotonic', side_effect=[0, 1, 11]), patch.object(cli, 'recv_frame', return_value={'command': 'vaults'}), patch.object(cli, 'execute', return_value=[]) as execute:
            session.serve(server, self.cfg, {}, 'original0000001', 10, lambda _: os.getuid())
        execute.assert_called_once(); server.accept.assert_called_once()

    def test_daily_deadline_caps_unlock(self):
        from datetime import datetime, timezone
        now = datetime(2099, 1, 1, 23, 59, 0, tzinfo=timezone.utc)
        with patch.object(demo, 'datetime') as clock:
            clock.now.return_value = now; clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(demo.lifetime({'_demo_reset_at': '2099-01-02T00:00:00Z'}, 3600), 60)
