#!/usr/bin/env python3
"""OAuth client protocol tests with a real loopback callback and mocked PocketBase."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from vaultcontext_client import auth as tc, session


class OAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'XDG_CACHE_HOME': str(Path(self.tmp.name) / 'cache'),
                                           'XDG_STATE_HOME': str(Path(self.tmp.name) / 'state')})
        self.env.start()
        os.environ.pop('VAULTCONTEXT_SESSION_DIR', None)
        self.addCleanup(self.env.stop)
        self.cfg = {'url': 'https://vault.example.com', 'email': 'member@example.com', 'password': 'unused-password'}
        self.record = {'collectionName': 'users', 'id': 'user00000000001', 'email': self.cfg['email']}
        self.auth = {'token': 'pocketbase-secret', 'record': self.record,
                     'meta': {'accessToken': 'google-secret', 'refreshToken': 'google-refresh'}}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]

    def flow(self, callback='success', auth=None, exchange_status=200, auth_url=None):
        captured, errors, threads, calls = {}, [], [], []
        def browser(url):
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                captured.update(query)
                redirect = query['redirect_uri'][0]
                if callback == 'timeout':
                    return
                if callback == 'wrong_state':
                    request = redirect + '?code=stolen-code&state=wrong'
                elif callback == 'denied':
                    request = redirect + '?' + urllib.parse.urlencode({'state': query['state'][0], 'error': 'access_denied'})
                else:
                    # Reject unrelated/malicious requests, then still accept the correct callback.
                    try:
                        urllib.request.urlopen(redirect + '?code=stolen-code&state=wrong')
                    except urllib.error.HTTPError as error:
                        self.assertEqual(error.code, 400)
                        error.close()
                    request = redirect + '?' + urllib.parse.urlencode({'state': query['state'][0], 'code': 'authorization-secret'})
                try:
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 400)
                    error.close()
            except Exception as error:
                errors.append(error)
        def say(message, *args):
            if message.startswith('Open this URL'):
                thread = threading.Thread(target=browser, args=(message.split('\n')[1],))
                threads.append(thread)
                thread.start()
        def send(cfg, method, path, body=None, token=None, **kwargs):
            calls.append((method, path, body, token))
            if path.endswith('/auth-methods'):
                return 200, {'oauth2': {'enabled': True, 'providers': [{'name': 'google', 'authURL': auth_url or 'https://accounts.google.com/o/oauth2/auth?client_id=example&redirect_uri='}]}}
            self.assertTrue(path.endswith('/auth-with-oauth2'))
            verifier = body['codeVerifier']
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            self.assertEqual(captured['code_challenge'], [challenge])
            self.assertEqual(captured['code_challenge_method'], ['S256'])
            self.assertEqual(captured['scope'], ['openid email profile'])
            self.assertEqual(body['code'], 'authorization-secret')
            self.assertEqual(body['redirectURL'], f'http://127.0.0.1:{self.port}/callback')
            self.assertNotIn('createData', body)
            return exchange_status, self.auth if auth is None else auth
        try:
            with patch.object(tc, 'send', side_effect=send), patch.object(tc, 'say', side_effect=say):
                result = tc.google_login(self.cfg, self.port, 1)
        finally:
            for thread in threads:
                thread.join(3)
            if errors:
                raise errors[0]
            # The listener is closed on both successful and failed authorization.
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', self.port))
        return result, calls

    def test_success_private_cache_and_pkce(self):
        result, calls = self.flow()
        self.assertEqual(result['method'], 'google')
        path = tc.session_file(self.cfg)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertNotIn('google-secret', path.read_text())
        self.assertNotIn('authorization-secret', path.read_text())
        self.assertNotIn('password', path.read_text())
        self.assertEqual(len(calls), 2)

    def test_session_lives_in_state_not_cache(self):
        tc.auth_session(self.cfg, self.auth, 'google')
        self.assertEqual(tc.session_file(self.cfg).parent, Path(self.tmp.name) / 'state' / 'vaultcontext')
        self.assertFalse((Path(self.tmp.name) / 'cache').exists())
        with patch.dict(os.environ, {'VAULTCONTEXT_SESSION_DIR': str(Path(self.tmp.name) / 'override')}):
            self.assertEqual(tc.session_file(self.cfg).parent, Path(self.tmp.name) / 'override')
            self.assertIsNone(tc.load_session(self.cfg))

    def write_legacy(self, token, mode=0o600):
        legacy = tc.legacy_session_file(self.cfg)
        legacy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        legacy.unlink(missing_ok=True)
        legacy.write_text(json.dumps({'url': self.cfg['url'], 'email': self.cfg['email'], 'token': token}))
        os.chmod(legacy, mode)
        return legacy

    def test_legacy_cache_session_moved_once(self):
        # A legacy file with unsafe permissions is neither trusted nor moved.
        legacy = self.write_legacy('unsafe-secret', 0o644)
        self.assertIsNone(tc.load_session(self.cfg))
        self.assertTrue(legacy.exists())
        self.assertFalse(tc.session_file(self.cfg).exists())
        legacy = self.write_legacy('legacy-secret')
        self.assertEqual(tc.load_session(self.cfg)['token'], 'legacy-secret')
        self.assertFalse(legacy.exists())
        self.assertEqual(stat.S_IMODE(tc.session_file(self.cfg).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(tc.session_file(self.cfg).parent.stat().st_mode), 0o700)
        self.assertEqual(tc.load_session(self.cfg)['token'], 'legacy-secret')
        # A saved session wins over a stale legacy file, and logout removes both.
        legacy = self.write_legacy('stale-secret')
        self.assertEqual(tc.load_session(self.cfg)['token'], 'legacy-secret')
        self.assertEqual(session.stop(self.cfg, logout=True), {'signed_out': True})
        self.assertFalse(legacy.exists() or tc.session_file(self.cfg).exists())

    def test_legacy_session_kept_when_save_fails(self):
        legacy = self.write_legacy('legacy-secret')
        with patch.object(tc.crypto, 'restore_file', side_effect=OSError(13, 'Permission denied')):
            self.assertEqual(tc.load_session(self.cfg)['token'], 'legacy-secret')
        self.assertTrue(legacy.exists())
        self.assertFalse(tc.session_file(self.cfg).exists())

    def test_denial_and_state_mismatch_never_exchange_or_cache(self):
        for callback in ('denied', 'wrong_state', 'timeout'):
            with self.subTest(callback=callback), self.assertRaises(tc.Fail):
                self.flow(callback)
            self.assertFalse(tc.session_file(self.cfg).exists())

    def test_reject_unexpected_identity(self):
        for record in ({**self.record, 'email': 'other@example.com'}, {**self.record, 'collectionName': '_superusers'}):
            with self.subTest(record=record), self.assertRaisesRegex(tc.Fail, 'unexpected identity'):
                self.flow(auth={'token': 'secret', 'record': record})
            self.assertFalse(tc.session_file(self.cfg).exists())

    def test_exchange_error_does_not_expose_provider_response(self):
        with self.assertRaises(tc.Fail) as error:
            self.flow(exchange_status=400)
        self.assertNotIn('google-secret', str(error.exception))
        self.assertFalse(tc.session_file(self.cfg).exists())

    def test_reject_non_google_authorization_url(self):
        for url in ('http://accounts.google.com/auth', 'https://evil.example/auth'):
            with self.subTest(url=url), self.assertRaisesRegex(tc.Fail, 'unexpected Google'):
                self.flow(auth_url=url)

    def test_refresh_persists_and_never_falls_back_to_password(self):
        tc.auth_session(self.cfg, self.auth, 'google')
        refreshed = {**self.auth, 'token': 'renewed-secret'}
        with patch.object(tc, 'send', side_effect=[(200, refreshed), (200, {'tables': []})]) as send:
            self.assertEqual(tc.call(self.cfg, 'GET', '/api/context/schema')[0], 200)
            self.assertEqual(send.call_args_list[1].args[4], 'renewed-secret')
        self.assertEqual(tc.load_session(self.cfg)['token'], 'renewed-secret')
        with patch.object(tc, 'send', return_value=(401, {})) as send, self.assertRaisesRegex(tc.Fail, 'vaultcontext login'):
            tc.call(self.cfg, 'POST', '/api/collections/issues/records', {'title': 'must not write'})
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args.args[2].endswith('/auth-refresh'))

    def test_missing_or_legacy_session_requires_explicit_login(self):
        for method in (None, 'password'):
            if method:
                tc.auth_session(self.cfg, self.auth, method)
            with self.subTest(method=method), patch.object(tc, 'send') as send:
                with self.assertRaisesRegex(tc.Fail, 'vaultcontext login'):
                    tc.call(self.cfg, 'GET', '/api/context/schema')
                send.assert_not_called()

    def test_refresh_identity_mismatch_preserves_previous_cache(self):
        tc.auth_session(self.cfg, self.auth, 'google')
        wrong = {**self.auth, 'token': 'wrong-user-token', 'record': {**self.record, 'email': 'other@example.com'}}
        with patch.object(tc, 'send', return_value=(200, wrong)) as send, self.assertRaisesRegex(tc.Fail, 'unexpected identity'):
            tc.call(self.cfg, 'GET', '/api/context/schema')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(tc.load_session(self.cfg)['token'], 'pocketbase-secret')

    def test_invalid_options_fail_before_network(self):
        for port, timeout in ((0, 10), (65536, 10), (8765, 0), (8765, 601)):
            with self.subTest(port=port, timeout=timeout), patch.object(tc, 'send') as send, self.assertRaises(tc.Fail):
                tc.google_login(self.cfg, port, timeout)
            send.assert_not_called()

    def test_refresh_throttle_and_expiry(self):
        def token(exp):
            payload = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).rstrip(b'=').decode()
            return 'header.' + payload + '.signature'
        with patch.object(tc.time, 'time', return_value=1000):
            tc.auth_session(self.cfg, {**self.auth, 'token': token(2000)}, 'google')
            with patch.object(tc, 'send', return_value=(200, {'tables': []})) as send:
                tc.call(self.cfg, 'GET', '/api/context/schema')
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[2], '/api/context/schema')
            with patch.object(tc, 'send', return_value=(200, self.auth)) as send:
                tc.call(self.cfg, 'POST', '/api/collections/users/auth-refresh')
            self.assertEqual(send.call_count, 1)
            self.assertTrue(tc.oauth_refresh_needed({'token': token(1050), 'refreshed_at': 999}))
            self.assertTrue(tc.oauth_refresh_needed({'token': token(2000), 'refreshed_at': 700}))
            self.assertFalse(tc.oauth_refresh_needed({'token': token(2000), 'refreshed_at': 701}))

    def test_nonstring_token_rejected(self):
        for token in (123, {'secret': 'value'}, ['secret']):
            with self.subTest(token=token), self.assertRaises(tc.Fail):
                tc.auth_session(self.cfg, {**self.auth, 'token': token}, 'google')
        self.assertFalse(tc.session_file(self.cfg).exists())

    def test_oauth_transport_error_is_sanitized(self):
        with patch.object(tc, 'send', side_effect=tc.Fail(1, 'redirect /?code=secret-code')):
            with self.assertRaises(tc.Fail) as error:
                tc.oauth_send(self.cfg, 'POST', '/api/collections/users/auth-with-oauth2')
        self.assertNotIn('secret-code', str(error.exception))

    def test_oauth_configuration_needs_no_password(self):
        with patch.dict(os.environ, {'VAULTCONTEXT_URL': self.cfg['url'], 'VAULTCONTEXT_USER_EMAIL': self.cfg['email']}):
            os.environ.pop('VAULTCONTEXT_USER_PASSWORD', None)
            self.assertNotIn('password', tc.config())
        with patch.dict(os.environ, {'VAULTCONTEXT_URL': self.cfg['url'], 'VAULTCONTEXT_USER_EMAIL': self.cfg['email'], 'VAULTCONTEXT_USER_PASSWORD': 'ignored'}):
            self.assertNotIn('password', tc.config())


if __name__ == '__main__':
    unittest.main()
