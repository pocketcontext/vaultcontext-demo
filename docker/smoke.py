#!/usr/bin/env python3
"""Checks of the VaultContext container image. Python 3, PyNaCl and the docker CLI.

  python3 docker/smoke.py smoke   --image IMAGE   start as ONCE does, provision an user, exercise REST and SQL, stop, start again
  python3 docker/smoke.py config  --image IMAGE   startup errors for missing or unusable Litestream configuration
  python3 docker/smoke.py restore --image IMAGE   replicate to MinIO, destroy container and volume, restore into an empty volume

Every step prints a `==>` line before it runs and every docker command is printed. Secrets reach docker through the
environment (`-e NAME`), never through the command line, and are masked in everything this script prints. On failure
the script prints the masked logs of every container it started, then removes its containers, volumes, and network.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
from vaultcontext_client import crypto
# Build the withdrawn community image from pinned upstream sources for CI only.
MINIO_IMAGE = os.environ.get('VAULTCONTEXT_TEST_MINIO_IMAGE', 'vaultcontext-minio-fixture:9e49d5e-7394ce0')
STOP_LIMIT = 60  # seconds. `docker stop` waits 10 seconds by default before it kills.
JWT = re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')

hidden = []      # secret values: masked in output, searched for in container logs
containers = []  # every container started, for the failure report and cleanup
volumes = []
networks = []


class Failure(Exception):
    pass


def secret(value):
    """Register a secret value. Returns it."""
    if value and value not in hidden:
        hidden.append(value)
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            print(f'::add-mask::{value}', flush=True)
    return value


def mask(text):
    for value in hidden:
        text = text.replace(value, '***')
    return JWT.sub('***JWT***', text)


def say(text=''):
    print(mask(text), flush=True)


def step(text):
    say(f'==> {text}')


def check(condition, text):
    if not condition:
        raise Failure(text)
    say(f'    ok: {text}')


def docker(*args, env=None, ok=True, timeout=300):
    """Run docker. Returns (exit status, stdout + stderr). `env` adds variables to docker's own environment."""
    say('    $ docker ' + ' '.join(args))
    try:
        done = subprocess.run(['docker', *args], env={**os.environ, **(env or {})}, text=True, errors='replace',
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise Failure(f'docker {args[0]} did not finish within {timeout} seconds. Output so far:\n{error.stdout or ""}')
    if ok and done.returncode != 0:
        raise Failure(f'docker {args[0]} exited with status {done.returncode}:\n{done.stdout}')
    return done.returncode, done.stdout


def logs(name):
    return docker('logs', name, ok=False)[1]


def state(name):
    """Returns (running, exit status)."""
    status, text = docker('inspect', '-f', '{{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}', name, ok=False)
    if status != 0:
        raise Failure(f'container {name} cannot be inspected:\n{text}')
    running, code, oom = text.split()
    check(oom == 'false', f'{name} was not killed for memory')
    return running == 'true', int(code)


def run_app(image, name, volume, env, network=None):
    """Start the image detached with a named volume at /storage and port 80 published on a free local port."""
    docker('volume', 'create', volume)
    volumes.append(volume)
    args = ['run', '-d', '--name', name, '-p', '127.0.0.1::80', '-v', f'{volume}:/storage']
    if network:
        args += ['--network', network]
    for key in env:
        args += ['-e', key]
    containers.append(name)
    docker(*args, image, env=env)


def http(method, url, body=None, token=None, headers=None):
    """Returns (status, headers, parsed JSON or text). Status 0 means no HTTP response."""
    request = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
    if body is not None:
        request.add_header('Content-Type', 'application/json')
    if token:
        request.add_header('Authorization', token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status, reply, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        status, reply, raw = error.code, error.headers, error.read()
    except (OSError, urllib.error.URLError) as error:
        return 0, {}, str(error)
    text = raw.decode(errors='replace')
    try:
        return status, reply, json.loads(text)
    except ValueError:
        return status, reply, text


def wait_up(name, limit=120):
    """Wait for GET /up. Fails early when the container exits. Returns the base URL."""
    step(f'waiting up to {limit} seconds for GET /up on {name}')
    deadline, last = time.time() + limit, ''
    while time.time() < deadline:
        running, code = state_quiet(name)
        if not running:
            raise Failure(f'{name} exited with status {code} before /up answered')
        base = base_url_quiet(name)
        if base:
            status, _, body = http('GET', base + '/up')
            last = f'status {status}, body {str(body)[:200]!r}'
            if status == 200:
                check(True, f'/up answered 200 without credentials at {base}')
                return base
        time.sleep(1)
    raise Failure(f'/up did not answer 200 within {limit} seconds; last answer: {last}')


def state_quiet(name):
    done = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}} {{.State.ExitCode}}', name], text=True, capture_output=True)
    if done.returncode != 0:
        raise Failure(f'container {name} cannot be inspected: {done.stderr}')
    running, code = done.stdout.split()
    return running == 'true', int(code)


def base_url_quiet(name):
    done = subprocess.run(['docker', 'port', name, '80/tcp'], text=True, capture_output=True)
    match = re.search(r'127\.0\.0\.1:(\d+)', done.stdout)
    return f'http://127.0.0.1:{match.group(1)}' if match else ''


def superuser_token(base, email, password):
    status, _, body = http('POST', base + '/api/collections/_superusers/auth-with-password', {'identity': email, 'password': password})
    if status != 200 or not isinstance(body, dict) or not body.get('token'):
        raise Failure(f'superuser login answered {status}: {str(body)[:500]}')
    check(True, 'superuser login with VAULTCONTEXT_SUPERUSER_EMAIL and VAULTCONTEXT_SUPERUSER_PASSWORD')
    return secret(body['token'])


def provision_user(base, token, email, password):
    status, _, body = http('POST', base + '/api/collections/users/records', token=token,
                           body={'name': 'Image check user', 'email': email, 'password': password, 'passwordConfirm': password})
    if status != 200 or not isinstance(body, dict) or not body.get('id'):
        raise Failure(f'creating the user with the superuser token answered {status}: {str(body)[:500]}')
    check(True, 'user created with the superuser token')
    return body['id']


class Client:
    """An ordinary default-users identity using REST writes and authenticated SQL reads."""

    def __init__(self, base, email, password, home):
        self.base = base
        status, _, body = http('POST', base + '/api/collections/users/auth-with-password',
                               {'identity': email, 'password': password})
        check(status == 200 and bool(body.get('token')), 'provisioned user can authenticate')
        self.token = secret(body['token'])
        self.user = body['record']

    def run(self, command, *args):
        if command == 'whoami':
            return json.dumps(self.user)
        if command == 'check':
            status, _, body = http('GET', self.base + '/api/context/schema', token=self.token)
        elif command == 'create':
            status, _, body = http('POST', self.base + '/api/collections/' + args[0] + '/records',
                                   json.loads(args[1]), token=self.token)
        else:
            raise Failure('unsupported smoke client operation')
        check(status == 200, f'{command} answered 200')
        return json.dumps(body)

    def sql(self, query):
        status, _, body = http('POST', self.base + '/api/context/query', {'sql': query}, token=self.token)
        check(status == 200, 'authenticated SQL query succeeded')
        return body['rows']


def action(client, op, payload):
    status, _, body = http('POST', client.base + '/api/collections/vault_actions/records',
                           {'op': op, 'payload': payload}, token=client.token)
    check(status == 200, f'{op} succeeded')
    return body['result']


def write_record(client, user_id):
    """Store real authenticated ciphertext, including multiple database chunks."""
    passphrase = secret('SyntheticVaultUnlockPassphrase123!')
    rows = client.sql(f"SELECT key_bundle FROM identity_secrets WHERE account = '{user_id}'")
    if rows:
        identity = crypto.unwrap_identity(json.loads(rows[0][0]), passphrase, user_id)
    else:
        identity = crypto.generate_identity()
        public = crypto.public_identity(identity)
        action(client, 'identity_init', {'public_key': public['enc_public'], 'signing_key': public['sign_public'],
               'fingerprint': crypto.fingerprint(public),
               'key_bundle': json.dumps(crypto.wrap_identity(identity, passphrase, user_id))})
    public = crypto.public_identity(identity)
    vault, document, version = (secrets.token_hex(8)[:15] for _ in range(3))
    key = crypto.new_vault_key()
    key_context = {'vault': vault, 'account': user_id, 'epoch': 1}
    metadata = json.dumps(crypto.encrypt_bytes(key, b'Synthetic image document', {'purpose': 'metadata', 'vault': vault}))
    action(client, 'vault_create', {'id': vault, 'metadata': metadata,
           'envelope': json.dumps(crypto.seal_key(key, public, identity, key_context))})
    original = bytes(range(256)) * 1300
    context = {'vault': vault, 'document': document, 'version': version, 'epoch': 1, 'revision': 1}
    encoded = json.dumps(crypto.encrypt_bytes(key, original, context))
    chunks = [encoded[i:i+200000] for i in range(0, len(encoded), 200000)]
    manifest = dict(context, sha256=hashlib.sha256(encoded.encode()).hexdigest())
    action(client, 'save', {'vault': vault, 'document': document, 'version': version,
           'expected_revision': 0, 'epoch': 1, 'metadata': metadata, 'manifest': json.dumps(manifest),
           'signature': crypto.sign_manifest(identity, manifest), 'chunks': chunks})
    return document, {'vault': vault, 'version': version, 'user': user_id, 'fingerprint': crypto.fingerprint(public)}


def check_records(client, document, expected):
    """Recover keys from server state and authenticate/decrypt recovered exact bytes."""
    user, vault, version = expected['user'], expected['vault'], expected['version']
    if 'archived' in expected:
        state = client.sql(f"SELECT archived, archive_revision, revision FROM documents WHERE id = '{document}'")
        check(state == [[1, 1, 1]], 'archived document state survives complete restore without changing content revision')
        audit = client.sql(f"SELECT action FROM audit_log WHERE target = '{document}' AND action = 'archive'")
        check(audit == [['archive']], 'document archive audit survives complete restore')
    rows = client.sql(f"SELECT key_bundle FROM identity_secrets WHERE account = '{user}'")
    check(len(rows) == 1, 'encrypted identity bundle survives restore')
    identity = crypto.unwrap_identity(json.loads(rows[0][0]), 'SyntheticVaultUnlockPassphrase123!', user)
    public = crypto.public_identity(identity)
    check(crypto.fingerprint(public) == expected['fingerprint'], 'restored key identity matches trusted fingerprint')
    rows = client.sql(f"SELECT envelope FROM key_envelopes WHERE vault = '{vault}' AND account = '{user}' AND epoch = 1")
    key = crypto.open_key(json.loads(rows[0][0]), identity, public, {'vault': vault, 'account': user, 'epoch': 1})
    rows = client.sql(f"SELECT manifest, signature FROM versions WHERE id = '{version}' AND document = '{document}'")
    check(len(rows) == 1, 'encrypted version survives persistence and restore')
    manifest, signature = json.loads(rows[0][0]), rows[0][1]
    crypto.verify_manifest(public, manifest, signature)
    chunks = client.sql(f"SELECT id, ciphertext, sha256 FROM version_chunks WHERE version = '{version}' ORDER BY position")
    check(len(chunks) > 1, 'all populated ciphertext chunks are restored')
    status, _, file_auth = http('POST', client.base + '/api/files/token', {}, token=client.token)
    check(status == 200, 'ordinary user can request protected file token')
    file_token = secret(file_auth['token'])
    parts = []
    for chunk_id, filename, checksum in chunks:
        request = urllib.request.Request(client.base + '/api/files/version_chunks/' + chunk_id + '/' + filename + '?token=' + file_token)
        with urllib.request.urlopen(request, timeout=15) as response:
            content = response.read()
        check(hashlib.sha256(content).hexdigest() == checksum, 'protected ciphertext chunk hash matches')
        parts.append(content.decode('utf-8'))
    encoded = ''.join(parts)
    check(hashlib.sha256(encoded.encode()).hexdigest() == manifest['sha256'], 'signed ciphertext checksum matches')
    context = {'vault': vault, 'document': document, 'version': version, 'epoch': 1, 'revision': 1}
    check(crypto.decrypt_bytes(key, json.loads(encoded), context) == bytes(range(256)) * 1300,
          'restored ciphertext decrypts to exact original binary bytes')


def check_logs(name, text=None):
    text = logs(name) if text is None else text
    found = [index for index, value in enumerate(hidden) if value in text]
    check(not found, f'the logs of {name} contain none of the {len(hidden)} secret values of this run (matches: {len(found)})')
    check(not JWT.search(text), f'the logs of {name} contain no token')
    return text


def stop(name):
    step(f'docker stop {name}: the server must exit by itself within {STOP_LIMIT} seconds with status 0')
    start = time.time()
    docker('stop', '-t', '60', name)
    elapsed = time.time() - start
    running, code = state(name)
    check(not running and elapsed < STOP_LIMIT, f'stopped in {elapsed:.1f} seconds')
    check(code == 0, f'exit status 0 (got {code}; 137 means it was killed, 143 that the signal was not handled)')


def once_env(extra=None):
    """The variables ONCE injects, with throwaway values, plus a superuser."""
    env = {
        'BASE_URL': 'https://vault.example.test', 'SECRET_KEY_BASE': secret(secrets.token_hex(32)), 'DISABLE_SSL': 'true', 'NUM_CPUS': '2',
        'SMTP_ADDRESS': 'smtp.example.test', 'SMTP_PORT': '587', 'SMTP_USERNAME': 'image-check', 'SMTP_PASSWORD': secret(secrets.token_urlsafe(24)),
        'MAILER_FROM_ADDRESS': 'Info <info@notifications.example.test>',
        'VAULTCONTEXT_SUPERUSER_EMAIL': 'operator@example.test', 'VAULTCONTEXT_SUPERUSER_PASSWORD': secret('-' + secrets.token_urlsafe(24)),
        'VAULTCONTEXT_GOOGLE_CLIENT_ID': 'image-test.apps.googleusercontent.com',
        'VAULTCONTEXT_GOOGLE_CLIENT_SECRET': secret(secrets.token_urlsafe(24)),
        'VAULTCONTEXT_GOOGLE_WORKSPACE_DOMAIN': 'example.test',
    }
    env.update(extra or {})
    return env


def public_assets(base):
    """Check the published image, not just the checkout used by browser tests."""
    for asset in ('index.html', 'styles.css', 'app.js', 'terms/index.html', 'privacy/index.html', 'vendor/pocketbase.es.mjs'):
        path = '/' if asset == 'index.html' else '/' + asset
        with urllib.request.urlopen(base + path, timeout=15) as response:
            check(response.status == 200, f'public asset {asset} is served')
            check(response.read() == (ROOT / 'pb_public' / asset).read_bytes(), f'public asset {asset} matches source')
            ancestor = "'none'"
            check('frame-ancestors ' + ancestor in response.headers.get('Content-Security-Policy', ''), f'frame policy for {asset}')
            check(response.headers.get('Referrer-Policy') == 'no-referrer', f'no referrer for {asset}')


def initialize(image, volume, env, network):
    docker('volume', 'create', volume)
    if volume not in volumes:
        volumes.append(volume)
    args = ['run', '--rm', '--network', network, '-v', volume + ':/storage']
    for key in env:
        args.extend(['-e', key])
    docker(*args, image, 'init', env=env, timeout=180)


def storage_fixture(tmp, run_id):
    """Disposable bucket-scoped S3 identities, never production configuration."""
    network = 'vc-storage-' + run_id
    minio = network + '-minio'
    if not os.environ.get('VAULTCONTEXT_TEST_MINIO_IMAGE'):
        docker('build', '--file', str(ROOT / 'docker/minio.Dockerfile'), '--tag', MINIO_IMAGE, str(ROOT / 'docker'), timeout=1200)
    docker('network', 'create', network); networks.append(network)
    root_key, root_secret = secret('root' + run_id), secret(secrets.token_hex(24))
    mc = {'MC_HOST_test': secret(f'http://{root_key}:{root_secret}@127.0.0.1:9000')}
    containers.append(minio)
    docker('run', '-d', '--name', minio, '--network', network,
           '-e', 'MINIO_ROOT_USER', '-e', 'MINIO_ROOT_PASSWORD', MINIO_IMAGE, 'server', '/data',
           env={'MINIO_ROOT_USER': root_key, 'MINIO_ROOT_PASSWORD': root_secret})
    for _ in range(60):
        status, _ = docker('exec', '-e', 'MC_HOST_test', minio, 'mc', 'mb', '--ignore-existing',
                           'test/files', 'test/replica', env=mc, ok=False)
        if status == 0: break
        time.sleep(1)
    else: raise Failure('synthetic MinIO startup failed')
    keys = {}
    for bucket in ('files', 'replica'):
        key, password = secret(bucket + run_id), secret(secrets.token_hex(24))
        keys[bucket] = (key, password)
        policy = {'Version':'2012-10-17', 'Statement':[{'Effect':'Allow','Action':['s3:*'],
                  'Resource':[f'arn:aws:s3:::{bucket}',f'arn:aws:s3:::{bucket}/*']}]}
        path = tmp / (bucket + '-policy.json'); path.write_text(json.dumps(policy))
        docker('cp', str(path), minio + ':/tmp/' + bucket + '-policy.json')
        docker('exec', '-e', 'MC_HOST_test', '-e', 'FIXTURE_USER', '-e', 'FIXTURE_PASSWORD', minio,
               'sh', '-c', 'mc admin user add test "$FIXTURE_USER" "$FIXTURE_PASSWORD"',
               env={**mc, 'FIXTURE_USER':key, 'FIXTURE_PASSWORD':password})
        docker('exec', '-e', 'MC_HOST_test', minio, 'mc', 'admin', 'policy', 'create',
               'test', bucket, '/tmp/' + bucket + '-policy.json', env=mc)
        docker('exec', '-e', 'MC_HOST_test', minio, 'mc', 'admin', 'policy', 'attach',
               'test', bucket, '--user', key, env=mc)
    env = once_env({'VAULTCONTEXT_S3_BUCKET':'files','VAULTCONTEXT_S3_ENDPOINT':f'http://{minio}:9000',
        'VAULTCONTEXT_S3_REGION':'us-east-1','VAULTCONTEXT_S3_ACCESS_KEY_ID':keys['files'][0],
        'VAULTCONTEXT_S3_SECRET_ACCESS_KEY':keys['files'][1], 'LITESTREAM_BUCKET':'replica',
        'LITESTREAM_PATH':'test/data','LITESTREAM_ENDPOINT':f'http://{minio}:9000',
        'LITESTREAM_REGION':'us-east-1','LITESTREAM_ACCESS_KEY_ID':keys['replica'][0],
        'LITESTREAM_SECRET_ACCESS_KEY':keys['replica'][1],'LITESTREAM_SYNC_INTERVAL':'1s'})
    return network, env


def smoke(image, tmp, run_id):
    name, volume = f'vc-smoke-{run_id}', f'vc-smoke-{run_id}'
    network, env = storage_fixture(tmp, run_id)
    user_email, user_password = 'user@example.test', secret(secrets.token_urlsafe(24))

    step('explicitly initializing the image, then starting with S3 and Litestream')
    initialize(image, volume, env, network)
    run_app(image, name, volume, env, network)
    volumes.remove(volume)
    base = wait_up(name)
    check(any('starting L0 retention monitor' in line and 'interval=5m0s' in line
              for line in logs(name).splitlines()),
          'Litestream runs expired L0 cleanup checks every five minutes')
    public_assets(base)
    check(docker('exec', name, 'cat', '/proc/1/comm')[1].strip() == 'tini', 'PID 1 is tini')

    step('settings taken from the environment, read with the superuser token')
    token = superuser_token(base, env['VAULTCONTEXT_SUPERUSER_EMAIL'], env['VAULTCONTEXT_SUPERUSER_PASSWORD'])
    status, _, settings = http('GET', base + '/api/settings', token=token)
    check(status == 200, f'GET /api/settings answered 200 (got {status})')
    check(settings['meta']['appURL'] == env['BASE_URL'], 'meta.appURL is BASE_URL')
    check(settings['smtp']['enabled'] is True and settings['smtp']['host'] == env['SMTP_ADDRESS'], 'SMTP is enabled with SMTP_ADDRESS as host')
    check(settings['rateLimits']['enabled'] is True, "rate limits are enabled by the image's default VAULTCONTEXT_RATE_LIMITS=true")
    check(env['SMTP_PASSWORD'] not in json.dumps(settings), 'the settings API does not return the SMTP password')

    status, _, collection = http('GET', base + '/api/collections/users', token=token)
    check(status == 200 and collection['oauth2']['enabled'], 'Google OAuth is enabled after migrations')
    check(collection['oauth2']['providers'][0]['clientId'] == env['VAULTCONTEXT_GOOGLE_CLIENT_ID'], 'Google client ID matches the environment')
    check(env['VAULTCONTEXT_GOOGLE_CLIENT_SECRET'] not in json.dumps(collection), 'the collection API does not return the Google secret')
    check(collection['createRule'] == "@request.context = 'oauth2'" and collection['passwordAuth']['enabled'], 'OAuth-only signup rule and password login are preserved')

    status, _, _ = http('POST', base + '/api/collections/users/records',
                         {'email': 'public@example.test', 'name': 'Public signup',
                          'password': 'SyntheticPublicPassword123!', 'passwordConfirm': 'SyntheticPublicPassword123!'})
    check(status in (400, 403), 'public REST signup is denied despite the OAuth-only signup rule')

    step('CORS: only BASE_URL is an allowed origin')
    _, reply, _ = http('GET', base + '/api/health', headers={'Origin': env['BASE_URL']})
    check(reply.get('Access-Control-Allow-Origin') == env['BASE_URL'], 'BASE_URL is allowed')
    _, reply, _ = http('GET', base + '/api/health', headers={'Origin': 'https://other.example.test'})
    check(reply.get('Access-Control-Allow-Origin') is None, 'another origin is not allowed')

    step('provisioning an user and exercising authenticated endpoints against the container')
    user_id = provision_user(base, token, user_email, user_password)
    client = Client(base, user_email, user_password, tmp / 'home-smoke')
    check(json.loads(client.run('whoami'))['id'] == user_id, 'ordinary user login returns the provisioned id')
    client.run('check')
    check(True, "authenticated schema endpoint is available")
    document, expected = write_record(client, user_id)
    check_records(client, document, expected)

    check_logs(name)
    stop(name)

    step('starting the same container again: the volume keeps the data and the superuser upsert is repeatable')
    docker('start', name)
    base = wait_up(name)
    client = Client(base, user_email, user_password, tmp / 'home-smoke-2')
    check_records(client, document, expected)
    text = check_logs(name)
    check('pbinstall' not in text, 'the logs contain no superuser installation link')
    step('freeze, restart, and thaw the real container with the existing operator token')
    token = superuser_token(base, env['VAULTCONTEXT_SUPERUSER_EMAIL'], env['VAULTCONTEXT_SUPERUSER_PASSWORD'])
    status, _, current = http('GET', base + '/api/context/maintenance', token=token)
    check(status == 200, 'maintenance state is available to the operator')
    status, _, frozen = http('PUT', base + '/api/context/maintenance',
                             {'readOnly': True, 'expectedGeneration': current['generation']}, token=token)
    check(status == 200 and frozen['state'] == 'read_only', 'runtime freeze drains writes')
    for identity in (token, client.token):
        status, _, _ = http('POST', base + '/api/collections/users/records', {}, token=identity)
        check(status == 503, 'ordinary and operator mutations are blocked')
    check_records(client, document, expected)
    marker = json.loads(docker('exec', name, 'cat', '/storage/pb_data/maintenance.json')[1])
    check(marker == {'readOnly': True, 'generation': frozen['generation']}, 'private durable freeze marker matches')
    stop(name)
    docker('start', name)
    base = wait_up(name)
    client.base = base
    status, _, current = http('GET', base + '/api/context/maintenance', token=token)
    check(status == 200 and current['state'] == 'read_only' and current['generation'] == frozen['generation'],
          'frozen container restart preserves state and the operator token')
    check_records(client, document, expected)
    status, _, thawed = http('PUT', base + '/api/context/maintenance',
                             {'readOnly': False, 'expectedGeneration': frozen['generation']}, token=token)
    check(status == 200 and thawed['state'] == 'writable', 'operator explicitly thaws after container restart')
    status, _, _ = http('POST', base + '/api/collections/users/records', {}, token=token)
    check(status == 400, 'post-thaw writes reach ordinary validation')
    stop(name)


def expect_startup_error(image, title, env, named, not_named=()):
    step(title)
    args = ['run', '--rm']
    for key in env:
        args += ['-e', key]
    status, text = docker(*args, image, env=env, ok=False, timeout=120)
    say('    output: ' + text.strip().replace('\n', '\n            '))
    check(status != 0, f'exit status is not 0 (got {status})')
    for variable in named:
        check(variable in text, f'the error names {variable}')
    for variable in not_named:
        check(variable not in text, f'the error does not name {variable}, which is set')
    check('Server started' not in text, 'the server did not start')
    check_logs('this run', text)


def config(image, tmp, run_id):
    storage = {'VAULTCONTEXT_S3_' + name: 'synthetic-' + name.lower() for name in
               ('BUCKET','ENDPOINT','REGION','ACCESS_KEY_ID','SECRET_ACCESS_KEY')}
    replica = {'LITESTREAM_' + name: 'replica-' + name.lower() for name in
               ('BUCKET','PATH','ACCESS_KEY_ID','SECRET_ACCESS_KEY')}
    good = dict(storage, **replica)
    for key in good:
        env = {name:value for name,value in good.items() if name != key}
        expect_startup_error(image, 'required configuration: ' + key, env, [key])
    for value in ('true', '1', 'false'):
        expect_startup_error(image, 'replication cannot be disabled',
                             dict(good, LITESTREAM_DISABLED=value), ['LITESTREAM_DISABLED'])
    for pair in (('VAULTCONTEXT_SUPERUSER_EMAIL','VAULTCONTEXT_SUPERUSER_PASSWORD'),
                 ('VAULTCONTEXT_GOOGLE_CLIENT_ID','VAULTCONTEXT_GOOGLE_CLIENT_SECRET')):
        for key in pair:
            expect_startup_error(image, 'paired credentials', dict(good, **{key:'synthetic'}), list(pair))
    for key in ('BUCKET', 'ACCESS_KEY_ID'):
        expect_startup_error(image, 'separate storage authority',
            dict(good, **{'VAULTCONTEXT_S3_'+key:good['LITESTREAM_'+key]}), ['separate'])


def restore(image, tmp, run_id):
    # The populated S3 gate is the production recovery contract, including archive
    # history, late writes, frozen restart and actual empty-volume recovery.
    import importlib.util
    spec = importlib.util.spec_from_file_location('object_storage_smoke', ROOT / 'docker/object_storage_smoke.py')
    drill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drill)
    drill.main(['--image', image, '--minio-image', MINIO_IMAGE])


def report_and_clean(failed):
    if failed:
        for name in containers:
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                say(f'::group::logs of {name}')
            text = logs(name)
            say(f'---- logs of {name} (masked) ----')
            say(text)
            say(docker('inspect', '-f', 'running={{.State.Running}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}', name, ok=False)[1])
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                say('::endgroup::')
    step('cleaning up')
    for name in containers:
        docker('rm', '-f', '-v', name, ok=False)
    for name in volumes:
        docker('volume', 'rm', '-f', name, ok=False)
    for name in networks:
        docker('network', 'rm', name, ok=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['smoke', 'config', 'restore'])
    parser.add_argument('--image', required=True, help='image reference that `docker run` can resolve locally')
    args = parser.parse_args()
    failed = True
    with tempfile.TemporaryDirectory(prefix='vaultcontext-image-') as tmp:
        try:
            {'smoke': smoke, 'config': config, 'restore': restore}[args.mode](args.image, Path(tmp), secrets.token_hex(3))
            failed = False
        except Failure as error:
            say(f'\nFAILED: {error}')
        except Exception as error:
            say(f'\nFAILED: unexpected {type(error).__name__}: {error}')
        finally:
            report_and_clean(failed)
    say('FAILED' if failed else f'PASSED: {args.mode}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
