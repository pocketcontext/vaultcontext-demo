#!/usr/bin/env python3
"""Synthetic read-after-write checks for reused filtered SQL snapshots in demo mode.

The pinned server retains each visitor's private snapshot while the source
database is unchanged; X-Context-Snapshot-At stays the same on a cache hit.
Onboarding, saves, another visitor's writes, disablement, a stale UTC generation,
restarts and a reset (fresh database) must each be reflected on the very next
read, and no visitor ever sees another visitor's rows. Data is opaque and synthetic.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import tempfile
import urllib.error
import urllib.request
from unittest.mock import patch

from demo_backend import contacts
from integration import server
from oauth_integration import google_fixture, REDIRECT

ADMIN = {'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!'}
TABLES = ('user_directory', 'identities', 'identity_secrets', 'vaults', 'memberships', 'key_envelopes',
          'documents', 'versions', 'version_chunks', 'invitations', 'audit_log')
COUNTS = ' UNION ALL '.join(f"SELECT '{table}', count(*) FROM {table}" for table in TABLES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    today = datetime.now(timezone.utc).date().isoformat()
    with contacts() as (contact_url, contact_token, _), google_fixture() as (url, codes), \
            tempfile.TemporaryDirectory(prefix='demo-freshness-') as tmp, patch.dict(os.environ, {
                'VAULTCONTEXT_DEMO_MODE': 'true', 'VAULTCONTEXT_DEMO_GENERATION': today,
                'VAULTCONTEXT_DEMO_CONTACT_URL': contact_url, 'VAULTCONTEXT_DEMO_CONTACT_TOKEN': contact_token}):
        state = Path(tmp) / 'pb_data'
        tokens, ids, stamps = {}, {}, {}

        @contextlib.contextmanager
        def running(data_dir):
            with server(args.binary, data_dir=data_dir) as request:
                yield request

        def sql(request, name, query=COUNTS, expected=200):
            req = urllib.request.Request(request.base_url + '/api/context/query', data=json.dumps({'sql': query}).encode(),
                                         method='POST', headers={'Content-Type': 'application/json', 'Authorization': tokens[name]})
            try:
                with urllib.request.urlopen(req, timeout=25) as response:
                    status, raw, headers = response.status, response.read(), response.headers
            except urllib.error.HTTPError as error:
                status, raw, headers = error.code, error.read(), error.headers
            assert status in (expected if isinstance(expected, tuple) else (expected,)), (name, status, raw.decode())
            if status != 200:
                return None, None
            assert headers['X-Context-Scope'] == 'authorized-snapshot'
            result = json.loads(raw)
            assert not result['truncated']
            return result['rows'], headers['X-Context-Snapshot-At']

        def view(request, name):
            rows, at = sql(request, name)
            return {table: count for table, count in rows if count}, at

        def warm(request, name):
            first, at = view(request, name)
            again, at2 = view(request, name)
            assert first == again and at == at2, ('expected a reused snapshot for an unchanged source', name)
            stamps[name] = at
            return first

        def fresh(request, name):
            result, at = view(request, name)
            assert at != stamps[name], ('a committed write must invalidate the reused snapshot', name)
            stamps[name] = at
            return result

        def vault_ids(request, name):
            return sorted(row[0] for row in sql(request, name, 'SELECT id FROM vaults')[0])

        def visitor(request, name, status):
            meta = request('GET', '/api/collections/users/auth-methods')['oauth2']['providers'][0]
            code, email = secrets.token_urlsafe(24), name + '@example.test'
            codes[code] = {'challenge': meta['codeChallenge'], 'user': {'sub': email, 'email': email, 'email_verified': True,
                                                                        'name': 'Synthetic visitor'}}
            auth = request('POST', '/api/collections/users/auth-with-oauth2', {'provider': 'google', 'code': code, 'redirectURL': REDIRECT,
                                                                                'codeVerifier': meta['codeVerifier']})
            tokens[name], ids[name] = auth['token'], auth['record']['id']
            # Onboarding: no SQL before enrollment, then the visitor's own rows on the next read.
            sql(request, name, expected=403)
            request('POST', '/api/demo/enroll', {'generation': today, 'termsVersion': status['termsVersion'], 'acceptTerms': True,
                                                 'salesConsent': False, 'newsletterConsent': False, 'expectedRevision': 0}, tokens[name])
            request('POST', '/api/collections/vault_actions/records', {'op': 'identity_init', 'payload': {
                'public_key': 'synthetic-public-' + name, 'signing_key': 'synthetic-signing-' + name,
                'fingerprint': 'synthetic-fingerprint-' + name, 'key_bundle': 'encrypted-bundle'}}, tokens[name])
            assert sql(request, name, 'SELECT id FROM user_directory')[0] == [[ids[name]]]

        def act(request, name, op, payload):
            request('POST', '/api/collections/vault_actions/records', {'op': op, 'payload': payload}, tokens[name])

        def save(vault, document, version):
            return {'vault': vault, 'document': document, 'version': version, 'expected_revision': 0, 'epoch': 1,
                    'metadata': 'encrypted-name', 'manifest': 'encrypted-manifest', 'signature': 'synthetic',
                    'chunks': ['synthetic-ciphertext']}

        own = {'user_directory': 1, 'identities': 1, 'identity_secrets': 1}
        with running(state) as request:
            admin = request('POST', '/api/collections/_superusers/auth-with-password', ADMIN)['token']
            request('PATCH', '/api/collections/users', {'oauth2': {'enabled': True, 'providers': [{
                'name': 'google', 'clientId': 'synthetic-client', 'clientSecret': 'synthetic-secret',
                'authURL': url + '/authorize', 'tokenURL': url + '/token', 'userInfoURL': url + '/userinfo'}]}}, admin)
            status = request('GET', '/api/demo/status')
            visitor(request, 'alice', status)
            visitor(request, 'bob', status)
            schema = request('GET', '/api/context/schema', token=tokens['alice'])
            assert schema['permissionModel'] == 'filtered-snapshot' and 'snapshot' not in schema, 'background snapshots must stay disabled'
            assert schema['snapshotLimits']['maxConcurrentPerRequester'] == 1, 'per-visitor admission must stay enabled'
            assert warm(request, 'alice') == own and warm(request, 'bob') == own

            # A later visitor's onboarding commits, invalidating retained copies without leaking rows.
            visitor(request, 'carol', status)
            assert fresh(request, 'alice') == own and fresh(request, 'bob') == own
            assert warm(request, 'carol') == own

            # A save reaches the owner's next read and nobody else's.
            act(request, 'alice', 'vault_create', {'id': 'a' * 15, 'metadata': 'encrypted', 'envelope': 'encrypted'})
            act(request, 'alice', 'save', save('a' * 15, 'd' * 15, 'v' * 15))
            alice = fresh(request, 'alice')
            assert all(alice[table] == 1 for table in ('vaults', 'memberships', 'key_envelopes', 'documents', 'versions', 'version_chunks')), alice
            assert alice['audit_log'] >= 1 and fresh(request, 'bob') == own and fresh(request, 'carol') == own
            warm(request, 'alice')

            # Another visitor's write invalidates the owner's copy; each still sees only their own vault.
            act(request, 'bob', 'vault_create', {'id': 'b' * 15, 'metadata': 'encrypted', 'envelope': 'encrypted'})
            assert fresh(request, 'alice') == alice
            assert vault_ids(request, 'alice') == ['a' * 15] and vault_ids(request, 'bob') == ['b' * 15]
            assert not sql(request, 'carol', 'SELECT id FROM vaults')[0]

            # Disablement is enforced before any retained copy is served.
            warm(request, 'carol')
            request('PATCH', '/api/collections/users/records/' + ids['carol'], {'disabled': True}, admin)
            sql(request, 'carol', expected=(401, 403))
            warm(request, 'alice')

        # A restart never reuses a copy built by the previous process.
        before = stamps['alice']
        with running(state) as request:
            rows, at = view(request, 'alice')
            assert rows == alice and at != before, 'restart must rebuild the snapshot'
            stamps['alice'] = at
            warm(request, 'alice')
            # A stale persisted UTC generation fences SQL even while a warm copy exists.
            admin = request('POST', '/api/collections/_superusers/auth-with-password', ADMIN)['token']
            request('PATCH', '/api/collections/demo_policy/records/demopolicy00001', {'generation': '2000-01-01'}, admin)
            sql(request, 'alice', expected=503)

        # After a reset (fresh database), pre-reset sessions cannot read any snapshot.
        with running(Path(tmp) / 'reset' / 'pb_data') as request:
            for name in ('alice', 'bob'):
                sql(request, name, expected=(401, 403))
    print('Demo snapshot reuse: onboarding, read-after-write, visitor isolation, disablement, stale generation, restart and reset checks passed')


if __name__ == '__main__':
    main()
