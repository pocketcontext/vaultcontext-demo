#!/usr/bin/env python3
"""Boundary-sized opaque files and metadata-only SQL over >64MiB ciphertext."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from synthetic_auth import cache_test_session
from integration import server

from vaultcontext_client import cli as vc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='vault-limits-') as temporary, server(args.binary) as request:
        root = Path(temporary)
        with patch.dict(os.environ, {'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_STATE_HOME': str(root / 'state')}):
            os.environ.pop('VAULTCONTEXT_SESSION_DIR', None)
            admin = request('POST', '/api/collections/_superusers/auth-with-password', {
                'identity': 'admin@example.com', 'password': 'SyntheticAdminPassword123!'})['token']
            password = 'SyntheticLimitsPassword123!'
            account = request('POST', '/api/collections/users/records', {
                'email': 'limits@example.test', 'name': 'Limits test', 'password': password, 'passwordConfirm': password}, admin)['id']
            cfg = {'url': request.base_url, 'email': 'limits@example.test', 'password': password}
            cache_test_session(cfg, cfg['password'])
            identity = vc.crypto.generate_identity()
            public = vc.crypto.public_identity(identity)
            fingerprint = vc.crypto.fingerprint(public)
            vc.action(cfg, 'identity_init', {'public_key': public['enc_public'], 'signing_key': public['sign_public'],
                      'fingerprint': fingerprint, 'key_bundle': vc.encode(vc.crypto.wrap_identity(identity, 'Synthetic unlock passphrase', account))})
            vc.verify_user(cfg, account, fingerprint)
            def run(command, **values):
                return vc.execute(cfg, identity, account, dict(command=command, **values))
            vault = run('create', name='Limits vault')['id']
            source = root / 'arbitrary.binary'
            source.write_bytes(bytes(range(256)) * (vc.crypto.MAX_FILE_SIZE // 256))
            assert source.stat().st_size == 8 * 1024 * 1024
            document = None
            versions = []
            checksums = []
            # Six 8MiB inputs exceed a 64MiB SQL snapshot if ciphertext were inline.
            for index in range(6):
                with source.open('r+b') as stream:
                    stream.write(bytes([index]))
                checksums.append(hashlib.sha256(source.read_bytes()).hexdigest())
                saved = run('save', vault=vault, document=document, path=str(source), name='arbitrary binary')
                document = saved['id']
                versions.append(saved['version'])
                assert saved['revision'] == index + 1
            stored = sum(path.stat().st_size for path in (request.data_dir / 'storage').rglob('*') if path.is_file())
            assert stored > 64 * 1024 * 1024, stored
            # FileField SQL exports filenames, never inline encrypted file bytes.
            summary = vc.query(cfg, 'SELECT count(*) AS chunks, sum(length(ciphertext)) AS filename_bytes FROM version_chunks')[0]
            assert summary['chunks'] > 300 and summary['filename_bytes'] < 64 * 1024
            listed = run('list', vault=vault)
            assert len(listed) == 1 and listed[0]['size'] == vc.crypto.MAX_FILE_SIZE
            assert len(run('history', document=document)) == 6
            for index in (0, 5):
                restored = root / ('restored-' + str(index))
                run('restore', document=document, version=versions[index], to=str(restored))
                assert hashlib.sha256(restored.read_bytes()).hexdigest() == checksums[index]
                assert restored.stat().st_mode & 0o777 == 0o600
            with source.open('ab') as stream:
                stream.write(b'x')
            with patch.object(vc, 'action') as write:
                try:
                    run('save', vault=vault, document=document, path=str(source))
                except ValueError:
                    pass
                else:
                    raise AssertionError('oversized input accepted')
                write.assert_not_called()
            assert len(run('history', document=document)) == 6
    print('PASS: six 8MiB versions, >64MiB protected ciphertext, metadata-only SQL, exact old/current restore, oversized input rejected before write')


if __name__ == '__main__':
    main()
