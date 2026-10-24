#!/usr/bin/env python3
"""Recover a live synthetic database and original into a separate isolated server."""
import argparse
import importlib.util
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
from unittest.mock import patch
from vaultcontext_client import cli as vc

from synthetic_auth import cache_test_session
from integration import ROOT, server


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())
    backup = load('complete_backup', ROOT / 'tests/legacy_backup.py')
    smoke = load('container_smoke', ROOT / 'docker/smoke.py')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='vaultcontext-recovery-') as tmp, server(binary) as request, patch.dict(os.environ):
        root = Path(tmp)
        os.environ['XDG_CACHE_HOME'] = str(root / 'cache')
        os.environ['XDG_STATE_HOME'] = str(root / 'state')
        os.environ.pop('VAULTCONTEXT_SESSION_DIR', None)
        admin = smoke.superuser_token(request.base_url, 'admin@example.com', 'SyntheticAdminPassword123!')
        password = 'SyntheticRestorePassword123!'
        owner = smoke.provision_user(request.base_url, admin, 'owner@example.test', password)
        foreign = smoke.provision_user(request.base_url, admin, 'foreign@example.test', password)
        client = smoke.Client(request.base_url, 'owner@example.test', password, None)
        doc, meta = smoke.write_record(client, owner)
        # Populate an ordinary client vault with active and archived documents,
        # authentic version history, and transitions independent of signed revisions.
        cfg = {'url': request.base_url, 'email': 'owner@example.test', 'password': password}
        cache_test_session(cfg, cfg['password'])
        bundle = vc.one(cfg, 'identity_secrets', 'account=' + vc.quote(owner))['key_bundle']
        identity = vc.crypto.unwrap_identity(json.loads(bundle), 'SyntheticVaultUnlockPassphrase123!', owner)
        vc.verify_user(cfg, owner, meta['fingerprint'])
        def run(command, **values):
            return vc.execute(cfg, identity, owner, dict(command=command, **values))
        vault = run('create', name='Recovery client fixture')['id']
        original = b'synthetic original\x00\xff'
        current = b'synthetic replacement without newline'
        source = root / 'source.binary'
        source.write_bytes(original)
        first = run('save', vault=vault, path=str(source))
        source.write_bytes(current)
        second = run('save', vault=vault, path=str(source), document=first['id'])
        run('archive', document=first['id'])
        run('unarchive', document=first['id'])
        archived = run('archive', document=first['id'])
        active = run('save', vault=vault, path=str(source), name='Active recovery document')
        history = run('history', document=first['id'])
        before = vc.query(cfg, 'SELECT * FROM documents WHERE vault=' + vc.quote(vault) + ' ORDER BY id')
        audit_before = vc.query(cfg, 'SELECT * FROM audit_log WHERE vault=' + vc.quote(vault) + ' ORDER BY id')
        backup.snapshot(request.data_dir, root / 'restored')
        backup.verify(root / 'restored')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        command = [binary, 'serve', '--dir=' + str(root / 'restored'), '--http=' + f'127.0.0.1:{port}',
                   '--hooksDir=' + str(ROOT / 'pb_hooks'), '--migrationsDir=' + str(ROOT / 'pb_migrations')]
        with open(root / 'restore.log', 'w+') as log:
            proc = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log)
            try:
                for _ in range(150):
                    try:
                        with urllib.request.urlopen(base + '/up', timeout=1) as response:
                            if response.status == 200: break
                    except OSError:
                        if proc.poll() is not None:
                            raise AssertionError('restored server failed to start')
                        time.sleep(.1)
                else:
                    raise AssertionError('restored server startup timeout')
                restored = smoke.Client(base, 'owner@example.test', password, None)
                smoke.check_records(restored, doc, meta)
                cfg = dict(cfg, url=base)
                cache_test_session(cfg, cfg['password'])
                vc.verify_user(cfg, owner, meta['fingerprint'])
                assert vc.query(cfg, 'SELECT * FROM documents WHERE vault=' + vc.quote(vault) + ' ORDER BY id') == before
                assert vc.query(cfg, 'SELECT * FROM audit_log WHERE vault=' + vc.quote(vault) + ' ORDER BY id') == audit_before
                assert archived['archive_revision'] == 3 and archived['revision'] == 2
                assert run('list', vault=vault)[0]['id'] == active['id']
                assert run('list', vault=vault, archived=True)[0]['id'] == first['id']
                assert len(run('list', vault=vault, all=True)) == 2
                assert run('history', document=first['id']) == history
                assert base64.b64decode(run('cat', document=first['id'])['data']) == current
                assert base64.b64decode(run('cat', document=first['id'], version=first['version'])['data']) == original
                destination = root / 'recovered-history'
                run('restore', document=first['id'], version=first['version'], to=str(destination))
                assert destination.read_bytes() == original
                archive = root / 'recovered-export'
                run('export', vault=vault, to=str(archive), export_passphrase='Synthetic archive passphrase')
                exported = vc.crypto.parse_export(vc.crypto.decrypt_export(json.loads(archive.read_text()), 'Synthetic archive passphrase'))
                entries = [entry for entry in exported['files'] if entry['document'] == first['id']]
                assert len(entries) == 2 and all(entry['archived'] for entry in entries)
                assert {base64.b64decode(entry['data']) for entry in entries} == {original, current}
                assert sum(not entry['archived'] for entry in exported['files']) == 1
                other = smoke.Client(base, 'foreign@example.test', password, None)
                assert other.sql('SELECT id FROM documents') == []
                status, _, token = smoke.http('POST', base + '/api/files/token', {}, token=other.token)
                assert status == 200
                chunk = restored.sql("SELECT id, ciphertext FROM version_chunks ORDER BY id LIMIT 1")[0]
                status, _, _ = smoke.http('GET', base + '/api/files/version_chunks/' + chunk[0] + '/' + chunk[1] + '?token=' + token['token'])
                assert status in (400, 403, 404)
            finally:
                proc.terminate()
                proc.wait(timeout=15)
        next(path for path in (root / 'restored/storage').rglob('*') if path.is_file()).unlink()
        try:
            backup.verify(root / 'restored')
        except RuntimeError:
            pass
        else:
            raise AssertionError('missing original did not fail verification')
    print(f'PASS: live DB/original snapshot, separate server recovery, archive state/history/export, ownership and checksums ({time.monotonic()-started:.1f}s)')


if __name__ == '__main__':
    main()
