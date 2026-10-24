#!/usr/bin/env python3
"""Installed package and optional copied launcher against an isolated server."""
import argparse
import hashlib
import json
import os
import socket
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch
from synthetic_auth import cache_test_session
from integration import server
from client_command import client_command
from cli_forward import terminal
from vaultcontext_client import cli as vc, crypto



def cat_checks(command, temporary, request, owner, other, document, historical, content):
    """Exercise verified bytes through the real broker and foreground CLI."""
    cfg, identity, account, fingerprint = owner
    stranger, _, _, _ = other
    env = dict(os.environ, VAULTCONTEXT_URL=cfg['url'],
               VAULTCONTEXT_USER_EMAIL=cfg['email'])

    def cat(*words, environment=env):
        return subprocess.run(command + ['cat', document, *words], cwd=temporary,
                              env=environment, capture_output=True, timeout=45)

    def rejected(result):
        assert result.returncode != 0 and result.stdout == b'', 'failed cat emitted contents'
        assert result.stderr and b'Traceback' not in result.stderr

    rejected(cat())  # A signed-in but locked user cannot read.
    with patch.object(vc, 'prompt_passphrase', return_value='synthetic passphrase'):
        vc.unlock(cfg, 180)
        vc.unlock(stranger, 180)
    try:
        result = cat()
        assert result.returncode == 0 and result.stdout == b'new bytes' and result.stderr == b''
        result = cat('--version', historical)
        assert result.returncode == 0 and result.stdout == content and result.stderr == b''
        assert len(result.stdout) == crypto.MAX_FILE_SIZE
        rejected(cat(environment=dict(env, VAULTCONTEXT_USER_EMAIL=stranger['email'])))

        # Empty documents are successful, zero-byte output; version IDs cannot cross documents.
        empty = Path(temporary) / 'empty'
        empty.write_bytes(b'')
        vault = vc.one(cfg, 'documents', 'id=' + vc.quote(document))['vault']
        saved = vc.execute(cfg, identity, account, dict(command='save', vault=vault, path=str(empty)))
        result = subprocess.run(command + ['cat', saved['id']], cwd=temporary, env=env,
                                capture_output=True, timeout=45)
        assert result.returncode == 0 and result.stdout == b'' and result.stderr == b''
        rejected(cat('--version', saved['version']))

        # Fingerprint checks are repeated during reading, even in an unlocked session.
        pinned = vc.pin_path(cfg).read_bytes()
        vc.pin_path(cfg).write_text('{}')
        try:
            result = cat()
            rejected(result)
            assert b'Unverified' in result.stderr
        finally:
            vc.pin_path(cfg).write_bytes(pinned)

        # Corrupt the final chunk of an isolated synthetic ciphertext. A failed read
        # must not leak a verified prefix before the full file has been checked.
        chunks = vc.query(cfg, 'SELECT ciphertext FROM version_chunks WHERE version=' +
                          vc.quote(historical) + ' ORDER BY position DESC LIMIT 1')
        paths = list(request.data_dir.joinpath('storage').rglob(chunks[0]['ciphertext']))
        assert len(paths) == 1
        ciphertext_path = paths[0]
        ciphertext = ciphertext_path.read_bytes()
        ciphertext_path.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
        try:
            rejected(cat('--version', historical))
        finally:
            ciphertext_path.write_bytes(ciphertext)

        # Both a buffered small write (flush) and a large write handle a closed
        # stdout pipe without a shutdown traceback or damage to the broker.
        for words in ([], ['--version', historical]):
            reader, writer = os.pipe()
            os.close(reader)
            try:
                result = subprocess.run(command + ['cat', document, *words], cwd=temporary,
                                        env=env, stdout=writer, stderr=subprocess.PIPE, timeout=45)
            finally:
                os.close(writer)
            assert result.returncode == 1 and b'Traceback' not in result.stderr
            assert b'Exception ignored' not in result.stderr
            assert vc.session_call(cfg, {'command': 'vaults'})[0]['id'] == vault

        # A client may also disconnect while the broker is fetching bytes.
        with socket.socket(socket.AF_UNIX) as abandoned:
            abandoned.connect(str(vc.socket_path(cfg)))
            abandoned.sendall((vc.encode(dict(command='cat', document=document, version=historical)) + '\n').encode())
        assert vc.session_call(cfg, {'command': 'vaults'})[0]['id'] == vault
        # An attached client that never drains a maximum-size response triggers
        # sendall's timeout, not BrokenPipeError/ConnectionResetError. The broker
        # must accept the following request after its five-second send timeout.
        with socket.socket(socket.AF_UNIX) as stalled:
            stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            stalled.connect(str(vc.socket_path(cfg)))
            stalled.sendall((vc.encode(dict(command='cat', document=document, version=historical)) + '\n').encode())
            started = time.monotonic()
            assert vc.session_call(cfg, {'command': 'vaults'})[0]['id'] == vault
            assert time.monotonic() - started >= 4, 'large broker response unexpectedly bypassed stalled reader'
        assert cat().stdout == b'new bytes'
    finally:
        vc.session_call(cfg, {'command': 'lock'})
        vc.session_call(stranger, {'command': 'lock'})
    print('VaultContext cat exact bytes, integrity, privacy and pipes: PASS')


def compare_checks(temporary, owner):
    """Real encrypted records: private fast comparisons and immutable legacy fallback."""
    cfg, identity, account, _ = owner
    def run(command, **kw):
        return vc.execute(cfg, identity, account, dict(command=command, **kw))
    vault = run('create', name='Synthetic comparison fixtures')['id']
    source = Path(temporary) / 'comparison.binary'
    original = b'synthetic opaque comparison bytes\x00\xff'
    source.write_bytes(original)
    saved = run('save', vault=vault, path=str(source))
    document = saved['id']
    def compare(**kw):
        return run('compare', document=document, path=str(source), **kw)
    expected = dict(document=document, version=saved['version'], same=True, method='encrypted-sha256')
    with patch.object(vc, 'download_chunk', side_effect=AssertionError('fast comparison downloaded content')):
        assert compare() == expected
        # Equal length alone cannot establish equality.
        source.write_bytes(b'X' + original[1:])
        assert compare() == dict(expected, same=False)
        source.write_bytes(b'')
        assert compare() == dict(expected, same=False)
    source.write_bytes(original)
    # Plaintext digests are absent from all persisted server record types and public results.
    digest = hashlib.sha256(original).hexdigest()
    for table in ('documents', 'versions', 'version_chunks', 'audit_log'):
        assert digest not in vc.encode(vc.rows(cfg, table)), table
    for result in (run('list', vault=vault), run('search', vault=vault, text='comparison'),
                   run('history', document=document), compare()):
        encoded = vc.encode(result)
        assert digest not in encoded and 'plaintext_sha256' not in encoded
    source.write_bytes(b'second synthetic version')
    run('save', vault=vault, path=str(source), document=document)
    assert compare()['same'] is True
    assert compare(version=saved['version']) == dict(expected, same=False)
    source.write_bytes(original)
    assert compare(version=saved['version']) == expected
    assert compare()['same'] is False
    run('archive', document=document)
    assert compare(version=saved['version']) == expected
    before = vc.rows(cfg, 'versions', 'document=' + vc.quote(document))
    # Simulate a historical client by omitting only the new encrypted metadata field.
    encrypt_metadata = vc.metadata
    def old_metadata(key, value, context):
        return encrypt_metadata(key, {k: v for k, v in value.items() if k != 'plaintext_sha256'}, context)
    with patch.object(vc, 'metadata', side_effect=old_metadata):
        legacy = run('save', vault=vault, path=str(source))
    with patch.object(vc, 'download_chunk', wraps=vc.download_chunk) as downloads:
        result = run('compare', document=legacy['id'], path=str(source))
        assert result == dict(document=legacy['id'], version=legacy['version'], same=True, method='legacy-download')
        assert downloads.call_count > 0
    source.write_bytes(b'different synthetic legacy bytes')
    assert run('compare', document=legacy['id'], path=str(source))['same'] is False
    assert vc.rows(cfg, 'versions', 'document=' + vc.quote(document)) == before
    assert len(run('history', document=legacy['id'])) == 1, 'comparison rewrote immutable history'
    source.write_bytes(b'')
    empty = run('save', vault=vault, path=str(source))
    assert run('compare', document=empty['id'], path=str(source)) == dict(
        document=empty['id'], version=empty['version'], same=True, method='encrypted-sha256')
    source.unlink()
    try:
        compare()
    except (vc.auth.Fail, OSError, ValueError):
        pass
    else:
        raise AssertionError('comparison accepted a missing local file')
    print('VaultContext encrypted checksum privacy, history and legacy comparisons: PASS')


def listing_rate_limit_checks(command, temporary, request, admin, owner):
    """Fifteen encrypted documents must list without exhausting the production SQL limit."""
    cfg, identity, account, _ = owner
    def run(command, **kwargs):
        return vc.execute(cfg, identity, account, dict(command=command, **kwargs))
    vault = run('create', name='Synthetic rate limit fixtures')['id']
    source = Path(temporary) / 'rate-limit.binary'
    source.write_bytes(b'synthetic rate-limit content')
    expected = set()
    for index in range(15):
        expected.add(run('save', vault=vault, path=str(source), name=f'rate-file-{index:02d}')['id'])
    run('change-passphrase', new_passphrase='Synthetic terminal passphrase 123!')
    env = dict(os.environ, VAULTCONTEXT_URL=cfg['url'],
               VAULTCONTEXT_USER_EMAIL=cfg['email'])
    terminal(command + ['unlock', '--timeout', '180'], env, temporary)
    request('PATCH', '/api/settings', {'rateLimits': {'enabled': True, 'rules': [
        {'label': '/api/context/', 'audience': '', 'duration': 10, 'maxRequests': 60},
    ]}}, admin)
    try:
        # Count actual source HTTP SQL requests, so retrying an inefficient listing
        # cannot masquerade as a successful batching regression check.
        for name, arguments in (('list', {}), ('search', {'text': 'rate-file-'})):
            with patch.object(vc.auth, 'send', wraps=vc.auth.send) as sends, patch.object(vc.auth.time, 'sleep') as sleeps:
                result = run(name, vault=vault, **arguments)
            assert {row['id'] for row in result} == expected
            sql_calls = [call for call in sends.call_args_list if call.args[2] == '/api/context/query']
            assert len(sql_calls) <= 8, f'{name} used {len(sql_calls)} SQL requests for 15 documents'
            sleeps.assert_not_called()
        for words in (['list', vault], ['search', vault, 'rate-file-']):
            result = subprocess.run(command + words, cwd=temporary, env=env,
                                    capture_output=True, timeout=45)
            assert result.returncode == 0, result.stderr
            assert {row['id'] for row in json.loads(result.stdout)} == expected
    finally:
        request('PATCH', '/api/settings', {'rateLimits': {'enabled': False}}, admin)
        vc.session_call(cfg, {'command': 'lock'})
    print('VaultContext 15-file listing/search under 60 SQL requests per 10 seconds: PASS')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True)
    parser.add_argument('--client',help='Copy and execute this standalone launcher outside the repository')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='vault-skill-') as tmp, server(args.binary) as req:
        command=client_command(args.client,tmp)
        os.environ['XDG_CACHE_HOME']=str(Path(tmp)/'cache');os.environ['XDG_STATE_HOME']=str(Path(tmp)/'state')
        os.environ.pop('VAULTCONTEXT_SESSION_DIR',None)
        admin=req('POST','/api/collections/_superusers/auth-with-password',{'identity':'admin@example.com','password':'SyntheticAdminPassword123!'})['token']
        users=[]
        for name in ['alice','bob','eve']:
            row=req('POST','/api/collections/users/records',{'email':name+'@example.com','name':name,'password':'SyntheticUserPassword123!','passwordConfirm':'SyntheticUserPassword123!'},admin)
            cfg={'url':req.base_url.replace('127.0.0.1', 'localhost'),'email':name+'@example.com','password':'SyntheticUserPassword123!'}
            cache_test_session(cfg, cfg['password'])
            identity=crypto.generate_identity();pub=crypto.public_identity(identity);fingerprint=crypto.fingerprint(pub)
            vc.action(cfg,'identity_init',{'public_key':pub['enc_public'],'signing_key':pub['sign_public'],'fingerprint':fingerprint,'key_bundle':vc.encode(crypto.wrap_identity(identity,'synthetic passphrase',row['id']))})
            vc.verify_user(cfg,row['id'],fingerprint)
            users.append((cfg,identity,row['id'],fingerprint))
        a,ai,aid,af=users[0]; b,bi,bid,bf=users[1]; e,ei,eid,ef=users[2]
        def run(cfg,identity,actor,command,**kw):return vc.execute(cfg,identity,actor,dict(command=command,**kw))
        vault=run(a,ai,aid,'create',name='Personal test')['id']
        source=Path(tmp)/'binary';content=b'\x00\xff\r\n'+os.urandom(crypto.MAX_FILE_SIZE-4);source.write_bytes(content)
        saved=run(a,ai,aid,'save',vault=vault,path=str(source),name='opaque binary')
        doc=saved['id'];ver=saved['version']
        assert run(a,ai,aid,'list',vault=vault)[0]['name']=='opaque binary'
        assert run(a,ai,aid,'search',vault=vault,text='binary')[0]['id']==doc
        out=Path(tmp)/'restored';run(a,ai,aid,'restore',document=doc,to=str(out));assert out.read_bytes()==content;assert out.stat().st_mode&0o777==0o600
        assert run(e,ei,eid,'vaults')==[]
        inv=run(a,ai,aid,'share',vault=vault,account=bid,role='reader',fingerprint=bf)['id']
        run(b,bi,bid,'accept',invitation=inv,fingerprint=af)
        bout=Path(tmp)/'bob';run(b,bi,bid,'restore',document=doc,to=str(bout));assert bout.read_bytes()==content
        try:run(b,bi,bid,'save',vault=vault,path=str(source))
        except vc.auth.Fail:pass
        else:raise AssertionError('reader wrote')
        run(a,ai,aid,'revoke',vault=vault,account=bid)
        assert run(b,bi,bid,'vaults')==[]
        assert run(a,ai,aid,'vaults')[0]['name']=='Personal test'
        source.write_bytes(b'new bytes')
        run(a,ai,aid,'save',vault=vault,path=str(source),document=doc)
        assert len(run(a,ai,aid,'history',document=doc))==2
        archived=run(a,ai,aid,'archive',document=doc)
        assert archived['archived'] is True
        assert run(a,ai,aid,'list',vault=vault)==[]
        assert run(a,ai,aid,'list',vault=vault,archived=True)[0]['id']==doc
        archive=Path(tmp)/'export';run(a,ai,aid,'export',vault=vault,to=str(archive),export_passphrase='separate archive passphrase')
        exported=json.loads(crypto.decrypt_export(json.loads(archive.read_text()),'separate archive passphrase'));assert len(exported['files'])==2
        assert exported['format']=='vaultcontext-export-v2'
        assert all(entry['archived'] is True for entry in exported['files'])
        restored_export=Path(tmp)/'archive-output'
        parsed=vc.parser().parse_args(['restore-export',str(archive),'--document',doc,'--version',ver,'--to',str(restored_export)])
        with patch.object(vc,'prompt_passphrase',return_value='separate archive passphrase'):vc.run(parsed)
        assert restored_export.read_bytes()==content
        run(a,ai,aid,'unarchive',document=doc)
        # Wrong public-key pins fail closed, even if the directory itself is valid.
        try:vc.verify_user(a,bid,'0'*64)
        except vc.auth.Fail:pass
        else:raise AssertionError('wrong fingerprint accepted')
        # The initial source-release CI still carries the previous launcher pin.
        # Source cat coverage is mandatory; a copied launcher is tested when it advertises cat.
        help_result=subprocess.run(command+['cat','--help'],cwd=tmp,capture_output=True)
        if not args.client or help_result.returncode == 0:
            assert help_result.returncode == 0, help_result.stderr
            cat_checks(command,tmp,req,users[0],users[2],doc,ver,content)
        else:
            print('Copied launcher predates cat; source cat checks run separately.')
        # Real fresh-process/socket lifecycle: no private material in cache, restore via broker, lock.
        with patch.object(vc,'prompt_passphrase',return_value='synthetic passphrase'):
            vc.unlock(a,30)
        assert vc.session_call(a,{'command':'vaults'})[0]['id']==vault
        cache=Path(tmp)/'cache'
        private_values=[identity[field].encode() for _,identity,_,_ in users for field in ('enc_private','sign_private')]
        # uv also uses XDG_CACHE_HOME and may fetch public schema/config JSON
        # containing the field name key_bundle. Actual private key values must
        # remain absent throughout that cache, including dependency checkouts.
        for path in cache.rglob('*.json'):
            value=path.read_bytes()
            assert not any(secret in value for secret in private_values), str(path.relative_to(cache))
        # Check all regular application cache files, regardless of extension:
        # neither plaintext keys nor an encrypted bundle may persist here.
        assert vc.auth.session_file(a).parent==Path(tmp)/'state'/'vaultcontext'
        assert not vc.auth.legacy_session_file(a).exists()
        for path in [*vc.auth.cache_file(a).parent.rglob('*'),*vc.auth.session_file(a).parent.rglob('*')]:
            if path.is_file():
                value=path.read_bytes()
                assert b'key_bundle' not in value and not any(secret in value for secret in private_values), path.name
        vc.session_call(a,{'command':'lock'})
        for _ in range(100):
            if not vc.socket_path(a).exists():break
            time.sleep(.01)
        # Session expiry is exercised with real time by cli_forward.py and
        # with a bounded in-process serving loop by the unit suite.
        # Validate schema snapshot against live schema.
        os.environ['VAULTCONTEXT_URL']=a['url'];os.environ['VAULTCONTEXT_USER_EMAIL']=a['email']
        vc.run(vc.parser().parse_args(['check']))
        result=subprocess.run(command+['check'],cwd=tmp,capture_output=True,text=True,check=True)
        assert json.loads(result.stdout)['compatible'] is True
        result=subprocess.run(command+['whoami'],cwd=tmp,capture_output=True,text=True,check=True)
        assert json.loads(result.stdout)['id']==aid
        compare_checks(tmp, users[0])
        listing_rate_limit_checks(command, tmp, req, admin, users[0])
        run(a,ai,aid,'change-passphrase',new_passphrase='replacement vault passphrase')
        bundle=json.loads(vc.one(a,'identity_secrets','account='+vc.quote(aid))['key_bundle'])
        assert crypto.unwrap_identity(bundle,'replacement vault passphrase',aid)==ai
        print('VaultContext packaged portable client: PASS')
if __name__=='__main__':main()
