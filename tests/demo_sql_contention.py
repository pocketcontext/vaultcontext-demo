#!/usr/bin/env python3
"""Bounded SQL contention diagnostic using isolated synthetic demo users.

Creates a loopback server, synthetic Google provider and disposable databases;
never accepts a live service URL. A contended result characterizes an availability
limitation rather than failing a regression assertion.
"""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import secrets
import statistics
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from demo_backend import contacts
from integration import ROOT, server
from oauth_integration import google_fixture, REDIRECT

SLOW_SQL = ('WITH RECURSIVE sequence(n) AS '
            '(SELECT 1 UNION ALL SELECT n+1 FROM sequence) SELECT sum(n) FROM sequence')


def measure(binary, expect_protected=False, uncapped=False):
    today = datetime.now(timezone.utc).date().isoformat()
    with tempfile.TemporaryDirectory(prefix='demo-contention-config-') as temporary:
        cwd = Path(temporary)
        config = json.loads((ROOT/'pocketcontext.json').read_text())
        if uncapped:
            config['snapshot'].pop('maxConcurrentPerRequester', None)
        (cwd/'pocketcontext.json').write_text(json.dumps(config))
        for folder in ('pb_public', 'skills'):
            shutil.copytree(ROOT/folder, cwd/folder)
        return measure_server(binary, today, cwd, expect_protected)


def measure_server(binary, today, cwd, expect_protected):
    with contacts() as (contact_url, contact_token, _), google_fixture() as (url, codes), patch.dict(os.environ, {
        'VAULTCONTEXT_DEMO_MODE': 'true', 'VAULTCONTEXT_DEMO_GENERATION': today,
        'VAULTCONTEXT_DEMO_CONTACT_URL': contact_url,
        'VAULTCONTEXT_DEMO_CONTACT_TOKEN': contact_token,
    }), server(binary, cwd=cwd) as request:
        admin = request('POST', '/api/collections/_superusers/auth-with-password', {
            'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!',
        })['token']
        request('PATCH', '/api/collections/users', {'oauth2': {'enabled': True, 'providers': [{
            'name': 'google', 'clientId': 'synthetic-client', 'clientSecret': 'synthetic-secret',
            'authURL': url+'/authorize', 'tokenURL': url+'/token', 'userInfoURL': url+'/userinfo',
        }]}}, admin)
        status = request('GET', '/api/demo/status')
        tokens = []
        for name in ('busy', 'ordinary'):
            meta = request('GET', '/api/collections/users/auth-methods')['oauth2']['providers'][0]
            code = secrets.token_urlsafe(24)
            email = name+'@example.test'
            codes[code] = {'challenge': meta['codeChallenge'], 'user': {
                'sub': email, 'email': email, 'email_verified': True, 'name': 'Synthetic visitor',
            }}
            token = request('POST', '/api/collections/users/auth-with-oauth2', {
                'provider': 'google', 'code': code, 'redirectURL': REDIRECT,
                'codeVerifier': meta['codeVerifier'],
            })['token']
            request('POST', '/api/demo/enroll', {
                'generation': today, 'termsVersion': status['termsVersion'], 'acceptTerms': True,
                'salesConsent': False, 'newsletterConsent': False, 'expectedRevision': 0,
            }, token)
            tokens.append(token)

        def query(token, sql):
            req = urllib.request.Request(request.base_url+'/api/context/query',
                data=json.dumps({'sql': sql}).encode(), headers={
                    'Content-Type': 'application/json', 'Authorization': token,
                }, method='POST')
            start = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    code, body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                code, body = error.code, json.load(error)
            return {'status': code, 'seconds': round(time.monotonic()-start, 3),
                    'message': body.get('message', ''), 'row_count': len(body.get('rows', []))}

        simple = 'SELECT id FROM user_directory'
        baseline = [query(tokens[1], simple) for _ in range(3)]
        assert all(item['status'] == 200 and item['row_count'] == 1 for item in baseline), baseline
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            busy = [pool.submit(query, tokens[0], SLOW_SQL) for _ in range(4)]
            # Let both global slots fill and the two further requests queue.
            time.sleep(0.25)
            ordinary = query(tokens[1], simple)
            background = [future.result() for future in busy]
        if expect_protected:
            assert ordinary['status'] == 200 and ordinary['seconds'] < 1, ordinary
            rejected = [item for item in background if item['status'] == 429]
            assert len(rejected) == 3 and all(item['seconds'] < 1 for item in rejected), background
            assert sum(item['status'] == 408 for item in background) == 1, background
        recovered = query(tokens[1], simple)
        assert recovered['status'] == 200 and recovered['row_count'] == 1, recovered
        return {'baseline_median_seconds': statistics.median(x['seconds'] for x in baseline),
                'ordinary_during_contention': ordinary, 'busy_requests': background,
                'ordinary_after_contention': recovered,
                'contended': ordinary['status'] != 200 or ordinary['seconds'] > 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--expect-protected', action='store_true',
                        help='assert prompt excess-request rejection and second-user progress')
    parser.add_argument('--uncapped', action='store_true',
                        help='remove only the requester cap in an isolated config for historical reproduction')
    args = parser.parse_args()
    if args.uncapped and args.expect_protected:
        parser.error('--uncapped conflicts with --expect-protected')
    print(json.dumps(measure(args.binary, args.expect_protected, args.uncapped), indent=2))


if __name__ == '__main__':
    main()
