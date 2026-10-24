#!/usr/bin/env python3
"""Store, version, restore and explicitly share encrypted files.

Private keys are stored encrypted on the server and decrypted only in memory.
"""
import argparse
import base64
import ctypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import resource
import re
import secrets
import socket
import stat
import struct
import sys
import textwrap
import urllib.parse
import urllib.request

from . import auth
from . import crypto
from . import keychain
from . import session
from . import demo

MAX_FRAME = 24 * 1024 * 1024

def encode(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True)

def uid():
    return ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(15))

def quote(value):
    return "'" + str(value).replace("'", "''") + "'"

def request(cfg, method, path, body=None):
    status, data = (auth.call_read_query(cfg, body)
                    if method == 'POST' and path == '/api/context/query'
                    else auth.call(cfg, method, path, body))
    if status >= 400:
        if status == 409 and method == 'POST' and path == '/api/collections/vault_actions/records' and isinstance(body, dict) and body.get('op') == 'identity_init':
            raise auth.Fail(4, 'Identity already initialized. Run vaultcontext unlock with your existing passphrase. init does not replace an identity or reset its passphrase.')
        if cfg.get('_demo_generation'):
            if status == 401:
                raise auth.Fail(1, 'Demo sign-in expired or was rejected. Run vaultcontext login using the same account you enrolled on the website.')
            if status == 403:
                raise auth.Fail(1, 'Demo access denied (HTTP 403). Check that the CLI uses the same Google account that accepted today’s terms at ' + cfg['url'] + '/. Sign-in alone is not enrollment. If already enrolled, check account access; do not recreate your identity.')
            if status == 409:
                raise auth.Fail(4, 'Demo state changed (HTTP 409). Reread the current state before retrying. This is not an enrollment error.')
            if status == 503:
                raise auth.Fail(1, 'Demo temporarily unavailable or resetting (HTTP 503). Wait and retry a read before making changes. After a daily reset, sign in and accept the new demo day’s terms on the website.')
        raise auth.Fail(4 if status == 409 else 1, f'HTTP {status}; operation failed. Reread state before retrying; response content suppressed.')
    return data

def query(cfg, sql):
    data = request(cfg, 'POST', '/api/context/query', {'sql': sql})
    if data.get('truncated'):
        raise auth.Fail(1, 'SQL response truncated; no partial result used.')
    return [row if isinstance(row, dict) else dict(zip(data['columns'], row)) for row in data['rows']]

def rows(cfg, table, where='1=1'):
    result = []
    offset = 0
    while True:
        page = query(cfg, f'SELECT * FROM {table} WHERE {where} ORDER BY id LIMIT 50 OFFSET {offset}')
        result.extend(page)
        if len(page) < 50:
            return result
        offset += len(page)

def one(cfg, table, where):
    values = rows(cfg, table, where)
    if len(values) != 1:
        raise auth.Fail(1, f'Expected one accessible {table} record.')
    return values[0]

def action(cfg, op, payload):
    return request(cfg, 'POST', '/api/collections/vault_actions/records', {'op': op, 'payload': payload})['result']

def whoami(cfg):
    return request(cfg, 'POST', '/api/collections/users/auth-refresh')['record']['id']

def public(row):
    return {'enc_public': row['public_key'], 'sign_public': row['signing_key']}

def pin_path(cfg):
    return auth.cache_file(cfg).with_suffix('.pins.json')

def pins(cfg):
    try:
        path = pin_path(cfg)
        with crypto._parent(path) as (directory, name):
            parent = os.fstat(directory)
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                raise auth.Fail(1, 'Unsafe fingerprint directory ownership or permissions.')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 65536):
                    raise auth.Fail(1, 'Unsafe fingerprint file ownership or permissions.')
                value = json.loads(stream.read(65537))
                after = os.fstat(stream.fileno())
                if (info.st_mtime_ns, info.st_ctime_ns, info.st_size) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
                    raise auth.Fail(1, 'Fingerprint file changed during read.')
                if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                    raise auth.Fail(1, 'Invalid fingerprint file.')
                return value
    except FileNotFoundError:
        return {}

def verify_user(cfg, account, fingerprint=None):
    row = one(cfg, 'identities', 'account=' + quote(account))
    actual = crypto.fingerprint(public(row))
    if actual != row['fingerprint']:
        raise auth.Fail(1, 'Directory fingerprint mismatch.')
    known = pins(cfg)
    if fingerprint is not None:
        if not secrets.compare_digest(fingerprint, actual):
            raise auth.Fail(1, 'Fingerprint does not match the independently verified value.')
        known[account] = actual
        path = pin_path(cfg)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with crypto._parent(path) as (directory, _):
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise auth.Fail(1, 'Unsafe fingerprint directory ownership or permissions.')
        crypto.restore_file(path, encode(known).encode(), overwrite=True)
    elif known.get(account) != actual:
        raise auth.Fail(1, f'Unverified or changed public key for {account}; verify-user with an independently obtained fingerprint first.')
    return public(row)

def key_context(vault, account, epoch, signer):
    return {'kind': 'vault-key', 'vault': vault, 'recipient': account, 'epoch': epoch, 'signer': signer}

def seal(cfg, identity, key, vault, account, epoch, signer):
    recipient = verify_user(cfg, account)
    return encode({'signer': signer, 'sealed': crypto.seal_key(key, recipient, identity, key_context(vault, account, epoch, signer))})

def vault_key(cfg, identity, account, vault, epoch, expected_signer=None):
    row = one(cfg, 'key_envelopes', f'vault={quote(vault)} AND account={quote(account)} AND epoch={int(epoch)}')
    envelope = json.loads(row['envelope'])
    signer = envelope['signer']
    owner = expected_signer or one(cfg, 'vaults', 'id=' + quote(vault))['owner']
    if signer != owner:
        raise auth.Fail(1, 'Vault-key envelope was not signed by its owner.')
    return crypto.open_key(envelope['sealed'], identity, verify_user(cfg, signer), key_context(vault, account, epoch, signer))

def metadata(key, value, context):
    return encode(crypto.encrypt_bytes(key, encode(value).encode(), context))

def decrypt_metadata(key, value, context):
    return json.loads(crypto.decrypt_bytes(key, json.loads(value), context))

def validate_file_metadata(info):
    if (not isinstance(info, dict) or not isinstance(info.get('name'), str) or
            type(info.get('size')) is not int or not 0 <= info['size'] <= crypto.MAX_FILE_SIZE):
        raise auth.Fail(1, 'Invalid file metadata.')
    if 'plaintext_sha256' in info and (not isinstance(info['plaintext_sha256'], str) or
                                      not re.fullmatch(r'[0-9a-f]{64}', info['plaintext_sha256'])):
        raise auth.Fail(1, 'Invalid file checksum metadata.')


def display_file_metadata(info):
    # Internal authenticated metadata is never expanded into user-facing JSON.
    return {'name': info['name'], 'size': info['size']}


def download_chunk(cfg, chunk):
    token = request(cfg, 'POST', '/api/files/token')['token']
    path = '/api/files/version_chunks/' + urllib.parse.quote(chunk['id'], safe='') + '/' + urllib.parse.quote(chunk['ciphertext'], safe='')
    url = cfg['url'] + path + '?' + urllib.parse.urlencode({'token': token})
    try:
        with auth.opener.open(urllib.request.Request(url, headers={'User-Agent': auth.USER_AGENT}), timeout=30) as response:
            value = response.read(262145)
        if len(value) > 262144 or hashlib.sha256(value).hexdigest() != chunk['sha256']:
            raise ValueError('invalid chunk')
        return value.decode('utf-8')
    except Exception:
        raise auth.Fail(1, 'Protected chunk download failed; sensitive details suppressed.') from None

def document_context(vault, document, version, epoch):
    return {'kind': 'file', 'vault': vault, 'document': document, 'version': version, 'epoch': epoch}

def verified_file_metadata(doc, ver, key, author):
    if ver['document'] != doc['id'] or ver['vault'] != doc['vault']:
        raise auth.Fail(1, 'Version/document mismatch.')
    manifest = json.loads(ver['manifest'])
    crypto.verify_manifest(author, manifest, ver['signature'])
    context = document_context(doc['vault'], doc['id'], ver['id'], int(ver['epoch']))
    if manifest['context'] != context or manifest['author'] != ver['author'] or manifest['revision'] != int(ver['revision']):
        raise auth.Fail(1, 'Signed manifest identity mismatch.')
    info = decrypt_metadata(key, manifest['metadata'], dict(context, kind='file-metadata'))
    validate_file_metadata(info)
    return info, context, manifest


def listing_metadata(cfg, identity, account, documents):
    # All caches are local to this command. A later command rechecks access and pins.
    if not documents:
        return []
    vault_id = documents[0]['vault']
    vault = one(cfg, 'vaults', 'id=' + quote(vault_id))
    keys, authors, result = {}, {}, []
    for offset in range(0, len(documents), 25):
        batch = documents[offset:offset + 25]
        selected = {doc['current_version'] for doc in batch}
        versions = query(cfg, 'SELECT * FROM versions WHERE id IN (' +
                         ','.join(quote(value) for value in sorted(selected)) + ') ORDER BY id LIMIT 25')
        by_id = {ver['id']: ver for ver in versions}
        if len(by_id) != len(versions) or set(by_id) != selected:
            raise auth.Fail(1, 'Incomplete or duplicate listing versions.')
        for doc in batch:
            ver = by_id[doc['current_version']]
            if doc['vault'] != vault_id or ver['document'] != doc['id'] or ver['vault'] != vault_id:
                raise auth.Fail(1, 'Version/document mismatch.')
            if int(doc['revision']) != int(ver['revision']):
                raise auth.Fail(1, 'Current document revision mismatch.')
            epoch = int(ver['epoch'])
            if epoch not in keys:
                keys[epoch] = vault_key(cfg, identity, account, vault_id, epoch, expected_signer=vault['owner'])
            if ver['author'] not in authors:
                authors[ver['author']] = verify_user(cfg, ver['author'])
            info, _, _ = verified_file_metadata(doc, ver, keys[epoch], authors[ver['author']])
            result.append((doc, info, ver))
    return result


def get_version(cfg, identity, account, document, version=None, content=True):
    doc = one(cfg, 'documents', 'id=' + quote(document))
    ver = one(cfg, 'versions', 'id=' + quote(version or doc['current_version']))
    if ver['document'] != document or ver['vault'] != doc['vault']:
        raise auth.Fail(1, 'Version/document mismatch.')
    if version is None and int(doc['revision']) != int(ver['revision']):
        raise auth.Fail(1, 'Current document revision mismatch.')
    key = vault_key(cfg, identity, account, doc['vault'], int(ver['epoch']))
    info, context, manifest = verified_file_metadata(doc, ver, key, verify_user(cfg, ver['author']))
    if not content:
        return None, info, ver
    # Fetch individual chunks: a file must not overflow the SQL result byte budget.
    chunks = query(cfg, f'SELECT id,position FROM version_chunks WHERE version={quote(ver["id"])} ORDER BY position LIMIT 100')
    if len(chunks) != int(ver['chunk_count']) or [int(c['position']) for c in chunks] != list(range(len(chunks))):
        raise auth.Fail(1, 'Incomplete file chunks.')
    payload = ''.join(download_chunk(cfg, one(cfg, 'version_chunks', 'id=' + quote(c['id']))) for c in chunks)
    if hashlib.sha256(payload.encode()).hexdigest() != manifest['sha256']:
        raise auth.Fail(1, 'File integrity check failed.')
    data = crypto.decrypt_bytes(key, json.loads(payload), context)
    if len(data) != info['size']:
        raise auth.Fail(1, 'File size mismatch.')
    if 'plaintext_sha256' in info and not secrets.compare_digest(hashlib.sha256(data).hexdigest(), info['plaintext_sha256']):
        raise auth.Fail(1, 'File checksum mismatch.')
    return data, info, ver

def execute(cfg, identity, account, args):
    demo.guard(cfg)
    command = args['command']
    if command == 'change-passphrase':
        secret = one(cfg, 'identity_secrets', 'account=' + quote(account))
        bundle = crypto.wrap_identity(identity, args['new_passphrase'], account)
        payload = {'key_bundle': encode(bundle), 'expected_revision': secret['revision']}
        payload['signature'] = crypto.sign_manifest(identity, dict(payload, account=account, purpose='identity-rewrap'))
        return action(cfg, 'identity_rewrap', payload)
    if command == 'create':
        vault, key = uid(), crypto.new_vault_key()
        return action(cfg, 'vault_create', {'id': vault, 'metadata': metadata(key, {'name': args['name']}, {'kind': 'vault-metadata', 'vault': vault}), 'envelope': seal(cfg, identity, key, vault, account, 1, account)})
    if command == 'vaults':
        result = []
        for vault in rows(cfg, 'vaults'):
            key = vault_key(cfg, identity, account, vault['id'], 1)
            result.append(dict(id=vault['id'], **decrypt_metadata(key, vault['metadata'], {'kind': 'vault-metadata', 'vault': vault['id']}), role=one(cfg, 'memberships', f'vault={quote(vault["id"])} AND account={quote(account)} AND active=1')['role']))
        return result
    if command == 'save':
        vault = one(cfg, 'vaults', 'id=' + quote(args['vault']))
        document, version = args.get('document') or uid(), uid()
        current = one(cfg, 'documents', 'id=' + quote(document)) if args.get('document') else None
        if current and current['vault'] != vault['id']:
            raise auth.Fail(1, 'Document belongs to another vault.')
        if current and current['archived']:
            raise auth.Fail(1, 'Document is archived; unarchive it before saving a new version.')
        revision = int(current['revision']) if current else 0
        archive_revision = int(current['archive_revision']) if current else 0
        epoch = int(vault['epoch'])
        key = vault_key(cfg, identity, account, vault['id'], epoch)
        data = crypto.read_file(args['path'])
        context = document_context(vault['id'], document, version, epoch)
        info = {'name': args['name'] if args.get('name') is not None else args['path'], 'size': len(data), 'plaintext_sha256': hashlib.sha256(data).hexdigest()}
        encrypted_info = metadata(key, info, dict(context, kind='file-metadata'))
        ciphertext = encode(crypto.encrypt_bytes(key, data, context))
        manifest = {'context': context, 'author': account, 'revision': revision + 1, 'metadata': encrypted_info, 'sha256': hashlib.sha256(ciphertext.encode()).hexdigest()}
        chunks = [ciphertext[i:i + 192 * 1024] for i in range(0, len(ciphertext), 192 * 1024)]
        return action(cfg, 'save', {'vault': vault['id'], 'document': document, 'version': version, 'expected_revision': revision, 'expected_archive_revision': archive_revision, 'epoch': epoch, 'metadata': encrypted_info, 'manifest': encode(manifest), 'signature': crypto.sign_manifest(identity, manifest), 'chunks': chunks})
    if command in ('archive', 'unarchive'):
        doc = one(cfg, 'documents', 'id=' + quote(args['document']))
        return action(cfg, command, {'vault': doc['vault'], 'document': doc['id'],
                                    'expected_revision': doc['revision'],
                                    'expected_archive_revision': doc['archive_revision']})
    if command in ('list', 'search'):
        result = []
        where = 'vault=' + quote(args['vault'])
        if not args.get('all'):
            where += ' AND archived=' + ('1' if args.get('archived') else '0')
        documents = rows(cfg, 'documents', where)
        for doc, info, ver in listing_metadata(cfg, identity, account, documents):
            if command != 'search' or args['text'].casefold() in info['name'].casefold():
                result.append(dict(id=doc['id'], revision=ver['revision'], archived=bool(doc['archived']), **display_file_metadata(info)))
        return result
    if command == 'history':
        result = []
        for ver in rows(cfg, 'versions', 'document=' + quote(args['document'])):
            _, info, checked = get_version(cfg, identity, account, args['document'], ver['id'], content=False)
            result.append(dict(version=checked['id'], revision=checked['revision'], author=checked['author'], **display_file_metadata(info)))
        return sorted(result, key=lambda v: v['revision'])
    if command == 'compare':
        _, info, ver = get_version(cfg, identity, account, args['document'], args.get('version'), content=False)
        local = crypto.read_file(args['path'])
        if 'plaintext_sha256' in info:
            same = len(local) == info['size'] and secrets.compare_digest(hashlib.sha256(local).hexdigest(), info['plaintext_sha256'])
            method = 'encrypted-sha256'
        else:
            # Pin the fallback to the version already selected, even if another save races us.
            stored, _, _ = get_version(cfg, identity, account, args['document'], ver['id'])
            same = secrets.compare_digest(local, stored)
            method = 'legacy-download'
        return {'document': args['document'], 'version': ver['id'], 'same': same, 'method': method}
    if command == 'cat':
        data, _, _ = get_version(cfg, identity, account, args['document'], args.get('version'))
        return {'data': base64.b64encode(data).decode('ascii')}
    if command == 'restore':
        data, _, ver = get_version(cfg, identity, account, args['document'], args.get('version'))
        crypto.restore_file(args['to'], data, overwrite=args.get('overwrite', False))
        return {'restored': ver['id'], 'bytes': len(data)}
    if command == 'members':
        return rows(cfg, 'memberships', 'vault=' + quote(args['vault']) + ' AND active=1')
    if command == 'share':
        verify_user(cfg, args['account'], args['fingerprint'])
        vault = one(cfg, 'vaults', 'id=' + quote(args['vault']))
        envelopes = [{'epoch': epoch, 'envelope': seal(cfg, identity, vault_key(cfg, identity, account, vault['id'], epoch), vault['id'], args['account'], epoch, account)} for epoch in range(1, int(vault['epoch']) + 1)]
        return action(cfg, 'share', {'vault': vault['id'], 'account': args['account'], 'role': args['role'], 'expected_revision': vault['revision'], 'envelopes': envelopes})
    if command == 'invitations':
        return rows(cfg, 'invitations', "status='pending'")
    if command == 'accept':
        invitation = one(cfg, 'invitations', 'id=' + quote(args['invitation']))
        verify_user(cfg, invitation['invited_by'], args['fingerprint'])
        offered = rows(cfg, 'key_envelopes', f'vault={quote(invitation["vault"])} AND account={quote(account)}')
        if not offered or sorted(int(e['epoch']) for e in offered) != list(range(1, len(offered) + 1)):
            raise auth.Fail(1, 'Incomplete invitation key history.')
        for envelope in offered:
            vault_key(cfg, identity, account, invitation['vault'], int(envelope['epoch']), expected_signer=invitation['invited_by'])
        return action(cfg, 'accept', {'invitation': invitation['id']})
    if command in ('revoke', 'rotate'):
        vault = one(cfg, 'vaults', 'id=' + quote(args['vault']))
        if command == 'revoke':
            action(cfg, 'revoke', {'vault': vault['id'], 'account': args['account'], 'expected_revision': vault['revision']})
            vault = one(cfg, 'vaults', 'id=' + quote(vault['id']))
        key, epoch = crypto.new_vault_key(), int(vault['epoch']) + 1
        members = rows(cfg, 'memberships', f'vault={quote(vault["id"])} AND active=1')
        envelopes = [{'account': m['account'], 'envelope': seal(cfg, identity, key, vault['id'], m['account'], epoch, account)} for m in members]
        return action(cfg, 'rotate', {'vault': vault['id'], 'expected_revision': vault['revision'], 'epoch': epoch, 'envelopes': envelopes})
    if command == 'export':
        files = []
        estimated_size = 128
        for doc in rows(cfg, 'documents', 'vault=' + quote(args['vault'])):
            for ver in rows(cfg, 'versions', 'document=' + quote(doc['id'])):
                data, info, _ = get_version(cfg, identity, account, doc['id'], ver['id'])
                estimated_size += ((len(data) + 2) // 3) * 4 + len(info['name'].encode('utf-8')) * 6 + 512
                if estimated_size > crypto.MAX_EXPORT_SIZE:
                    raise auth.Fail(1, 'Vault exceeds the 64 MiB plaintext archive limit.')
                files.append({'document': doc['id'], 'version': ver['id'], 'revision': ver['revision'], 'name': info['name'], 'archived': bool(doc['archived']), 'data': base64.b64encode(data).decode()})
        encrypted = crypto.encrypt_export(encode({'format': 'vaultcontext-export-v2', 'files': files}).encode(), args['export_passphrase'])
        crypto.restore_file(args['to'], encode(encrypted).encode(), overwrite=args.get('overwrite', False))
        return {'exported_versions': len(files)}
    raise auth.Fail(2, 'Unknown vault operation.')

# Local session: same-uid Unix socket, private directory, fixed lifetime, no key files.
def peer_uid_reader():
    """Resolve the native credential API before starting a memory session."""
    if sys.platform.startswith('linux') and hasattr(socket, 'SO_PEERCRED'):
        def linux_uid(conn):
            return struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        return linux_uid
    if sys.platform == 'darwin':
        # Darwin uid_t/gid_t are unsigned 32-bit integers. Python's socket
        # module does not expose getpeereid, so call the system libc directly.
        getpeereid = ctypes.CDLL(None, use_errno=True).getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32),
                              ctypes.POINTER(ctypes.c_uint32)]
        getpeereid.restype = ctypes.c_int
        def darwin_uid(conn):
            user, group = ctypes.c_uint32(), ctypes.c_uint32()
            if getpeereid(conn.fileno(), ctypes.byref(user), ctypes.byref(group)) != 0:
                raise OSError(ctypes.get_errno(), 'Cannot verify unlock session peer.')
            return user.value
        return darwin_uid
    raise auth.Fail(1, 'Unlock sessions require Linux or macOS peer credentials.')

def socket_path(cfg):
    return auth.cache_file(cfg).with_suffix('.sock')

def recv_frame(conn):
    data = bytearray()
    while b'\n' not in data:
        part = conn.recv(65536)
        if not part:
            raise ValueError('Incomplete session request')
        data.extend(part)
        if len(data) > MAX_FRAME:
            raise ValueError('Session request too large')
    return json.loads(bytes(data).split(b'\n', 1)[0])

def session_call(cfg, payload, timeout=180):
    path = socket_path(cfg)
    session.socket_info(path)
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(timeout)
        conn.connect(str(path))
        if peer_uid_reader()(conn) != os.getuid():
            raise auth.Fail(1, 'Unsafe unlock session peer.')
        conn.sendall((encode(payload) + '\n').encode())
        result = recv_frame(conn)
    if 'error' in result:
        raise auth.Fail(result.get('code', 1), result['error'])
    return result['result']

def prompt_passphrase(confirm=False):
    if not sys.stdin.isatty():
        raise auth.Fail(2, 'Run this command in an interactive terminal; passphrases are never accepted through arguments, environment variables or stdin pipes.')
    value = getpass.getpass('Passphrase: ')
    if len(value) < 12:
        raise auth.Fail(2, 'Use a passphrase of at least 12 characters.')
    if confirm and value != getpass.getpass('Confirm passphrase: '):
        raise auth.Fail(2, 'Passphrases did not match.')
    return value

def keychain_enroll(cfg):
    keychain.require_helper()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    account = whoami(cfg)
    secret = one(cfg, 'identity_secrets', 'account=' + quote(account))
    passphrase = prompt_passphrase()
    identity = crypto.unwrap_identity(json.loads(secret['key_bundle']), passphrase, account)
    verify_user(cfg, account, crypto.fingerprint(crypto.public_identity(identity)))
    # Enrollment never caches the identity bundle or starts a session.
    keychain.call(cfg, account, 'store', passphrase)
    return {'keychain_enrolled': True}


def unlock(cfg, timeout, use_keychain=False):
    if not 30 <= timeout <= 3600:
        raise auth.Fail(2, 'Unlock lifetime must be 30–3600 seconds.')
    timeout = demo.lifetime(cfg, timeout)
    peer_uid_reader()
    if use_keychain:
        keychain.require_helper()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    account = whoami(cfg)
    secret = one(cfg, 'identity_secrets', 'account=' + quote(account))
    passphrase = keychain.call(cfg, account, 'get') if use_keychain else prompt_passphrase()
    try:
        identity = crypto.unwrap_identity(json.loads(secret['key_bundle']), passphrase, account)
    except Exception:
        if use_keychain:
            raise auth.Fail(1, 'Saved Keychain credential could not unlock the server identity. Use vaultcontext unlock, then enroll again if your passphrase changed.') from None
        raise
    finally:
        # Only the verified identity crosses private IPC, never the passphrase.
        # Python does not guarantee erasure of runtime copies.
        del passphrase
    verify_user(cfg, account, crypto.fingerprint(crypto.public_identity(identity)))
    return session.start(cfg, identity, account, timeout)

class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        if 'unrecognized arguments:' in message and '--google' in message.split():
            message = '--google has been removed. Use vaultcontext login for Google sign-in.'
        super().error(message)


def parser():
    p = CommandParser(
        prog='vaultcontext',
        description=__doc__, usage='%(prog)s [-h] COMMAND ...',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""First use (replace the example URL and email):
  export VAULTCONTEXT_URL=https://vault.example.com
  export VAULTCONTEXT_USER_EMAIL=you@example.com
  vaultcontext login
  vaultcontext init                         # once per account; prompts for a passphrase
  vaultcontext unlock                       # prompts; session lasts 15 minutes
  vaultcontext create 'Personal'            # returns the VAULT_ID for save
  vaultcontext save VAULT_ID /path/to/file
  vaultcontext lock

Google login authenticates your account; unlock decrypts your keys.
Passphrases require an interactive terminal. There are no recovery keys.
Commands return JSON except cat, which writes exact file bytes to stdout.
Run vaultcontext COMMAND --help for arguments and examples.
Manual: https://github.com/pocketcontext/vaultcontext-demo/blob/main/skills/vaultcontext/references/workflows.md""")
    commands = p.add_subparsers(prog=p.prog, dest='command', required=True, metavar='COMMAND',
                               help=argparse.SUPPRESS)
    summaries = {}
    positional_help = {
        'vault': ('VAULT_ID', 'vault ID from vaultcontext vaults or vaultcontext create'),
        'document': ('DOCUMENT_ID', 'document ID from vaultcontext list or vaultcontext search'),
        'account': ('ACCOUNT_ID', 'account ID from vaultcontext directory or vaultcontext members'),
        'invitation': ('INVITATION_ID', 'invitation ID from vaultcontext invitations'),
        'path': ('PATH', 'source file path; at most 8 MiB; symlinks rejected'),
        'name': ('NAME', 'display name for the new vault (encrypted on the server)'),
        'text': ('TEXT', 'case-insensitive fragment of the decrypted display name'),
        'archive': ('ARCHIVE', 'local encrypted archive created by vaultcontext export'),
    }

    def add(name, summary, description, example, *positionals):
        summaries[name] = summary
        q = commands.add_parser(
            name, description='\n\n'.join(textwrap.fill(part, width=76) for part in description.split('\n\n')),
            epilog='Example:\n  vaultcontext ' + example,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        for arg in positionals:
            metavar, help_text = positional_help[arg]
            q.add_argument(arg, metavar=metavar, help=help_text)
        return q

    def archive_filters(q):
        group = q.add_mutually_exclusive_group()
        group.add_argument('--archived', action='store_true', help='include only archived documents')
        group.add_argument('--all', action='store_true', help='include active and archived documents')

    def destination(q):
        q.add_argument('--to', required=True, metavar='PATH',
                       help='destination file; parent directory must exist; symlinks rejected; mode 0600')
        q.add_argument('--overwrite', action='store_true',
                       help='replace an existing destination (default: refuse overwrite)')

    def fingerprint(q, person):
        q.add_argument('--fingerprint', required=True, metavar='FINGERPRINT',
                       help=f'{person} public-key fingerprint, verified through an independent trusted channel')

    unlocked = 'Requires login and an unlocked session (vaultcontext unlock). '
    q = add('login', 'Sign in to your account',
            'Sign in and cache an application token. Set VAULTCONTEXT_URL and '
            'VAULTCONTEXT_USER_EMAIL first. Google browser sign-in is the only login method. '
            'Signing in does not unlock the vault; its passphrase remains separate.\n\n'
            'Over SSH, forward the callback port: ssh -L 8765:127.0.0.1:8765 user@host. '
            'Open the printed URL on your browser machine; decryption stays on the CLI host.',
            'login')
    q.add_argument('--port', type=int, default=8765,
                   help='Google callback port, 1–65535; (default: %(default)s)')
    q.add_argument('--timeout', type=int, default=180,
                   help='Google sign-in wait in seconds, 1–600; (default: %(default)s)')
    add('whoami', 'Show your authenticated account ID',
        'Refresh the application token and return your account ID. Requires login; no unlock needed.', 'whoami')
    add('logout', 'Lock and remove the local sign-in token',
        'Close the local unlock session and delete the saved application token for the configured '
        'server and email, including any copy left in the old cache folder. Copied tokens are not revoked.', 'logout')
    add('check', 'Check server schema compatibility',
        'Compare the authenticated server SQL schema with this client\'s bundled snapshot. '
        'Requires login; no unlock needed. Exits with code 3 for schema differences.', 'check')
    add('init', 'Initialize your encryption identity once',
        'Requires login. Create your encryption/signing identity once per account. '
        'Prompts twice for a passphrase of at least 12 characters in an interactive terminal; '
        'stores the encrypted private-key bundle on the server and returns your public fingerprint. '
        'There are no recovery keys. Run vaultcontext unlock afterward.', 'init')
    q = add('unlock', 'Start a temporary session with keys in memory',
            'Requires login and an initialized identity (vaultcontext init). Starts a same-OS-user memory '
            'session. By default, prompts for your vault passphrase in an interactive terminal; '
            '--keychain uses an enrolled credential after macOS authentication. '
            'The execution host and agents using the session are trusted. Expiration rejects '
            'new requests; an operation already running may finish. Close early with vaultcontext lock.',
            'unlock --timeout 900')
    q.add_argument('--timeout', type=int, default=900,
                   help='session lifetime in seconds, 30–3600 (default: %(default)s)')
    q.add_argument('--keychain', action='store_true',
                   help='retrieve an enrolled passphrase with macOS authentication; requires the signed helper; no automatic fallback')
    add('keychain-enroll', 'Remember your passphrase in macOS Keychain',
        'Requires login and the signed macOS helper. Prompt for your existing passphrase in an '
        'interactive terminal, verify the server identity, then save the passphrase in a '
        'non-synchronizing Keychain item protected by macOS user presence. Does not open a session.',
        'keychain-enroll')
    add('keychain-forget', 'Remove this account\'s saved macOS passphrase',
        'Requires login and the signed macOS helper. Delete the Keychain credential scoped to '
        'this server and account. Does not end an existing session; use vaultcontext lock separately. '
        'Lock and logout retain enrollment.', 'keychain-forget')
    add('lock', 'Close the local memory session',
        'End the unlocked session for the configured server and email. Keeps the application '
        'token; use vaultcontext logout to remove it too. Safely clears stale sessions.', 'lock')
    add('change-passphrase', 'Change your identity passphrase',
        unlocked + 'Prompt twice for a new passphrase (at least 12 characters) in an interactive '
        'terminal and re-encrypt the server identity bundle. This does not replace identity keys. '
        'An existing unlocked session can change a forgotten passphrase; there are no recovery keys. '
        'Saved Keychain credentials are not updated; run vaultcontext keychain-enroll again afterward.',
        'change-passphrase')
    add('vaults', 'List accessible vault names, IDs and roles',
        unlocked + 'Decrypt vault names and list your active vault memberships.', 'vaults')
    add('create', 'Create a private vault',
        unlocked + 'Create a vault you own and return its ID. Use vaultcontext share to invite members.',
        "create 'Personal'", 'name')
    q = add('save', 'Save a file or add an immutable version',
            unlocked + 'Owners and editors can save exact file bytes, up to 8 MiB. By default '
            'creates a new document; --document adds an immutable version to an existing document '
            'in the same vault. Archived documents must be unarchived first. Names are encrypted; '
            'the default preserves the exact source path argument as a label, without normalization. '
            'Names are not unique and never select restore destinations. Symlink sources and ancestors are rejected.',
            'save VAULT_ID /path/to/file --document DOCUMENT_ID', 'vault', 'path')
    q.add_argument('--document', metavar='DOCUMENT_ID',
                   help='existing document ID from vaultcontext list; omit to create a new document')
    q.add_argument('--name', metavar='NAME',
                   help='encrypted display name for this version (default: exact source path argument)')
    q = add('list', 'List files and document IDs in a vault',
        unlocked + 'Authenticate current versions and show decrypted names, sizes, revisions and '
        'archive status. Lists active documents by default. Does not print file contents. '
        'Verify other writers with vaultcontext verify-user first.', 'list VAULT_ID --all', 'vault')
    archive_filters(q)
    q = add('search', 'Search decrypted file names',
        unlocked + 'Search current display names locally, ignoring case. Searches active documents '
        'by default. Does not search file contents. Verify other writers with vaultcontext verify-user first.',
        'search VAULT_ID project --archived', 'vault', 'text')
    archive_filters(q)
    add('archive', 'Hide a document from active file listings',
        unlocked + 'Owners and editors can archive an entire document and all its versions. '
        'Repeated requests are harmless. Blocks new versions until unarchived; history, cat and '
        'restore remain available by ID. Does not delete data, reclaim storage or revoke access. '
        'Exports and whole-vault sharing still include archived documents.',
        'archive DOCUMENT_ID', 'document')
    add('unarchive', 'Return an archived document to active listings',
        unlocked + 'Owners and editors can unarchive a document, allowing new versions again. '
        'Repeated requests are harmless. Preserves all retained versions.',
        'unarchive DOCUMENT_ID', 'document')
    add('history', 'List a document\'s retained versions',
        unlocked + 'Authenticate retained versions and show version IDs, revisions, authors, '
        'names and sizes. Verify other writers with vaultcontext verify-user first.', 'history DOCUMENT_ID', 'document')
    q = add('compare', 'Compare a local file with a stored version',
            unlocked + 'Verify signed encrypted metadata and compare a local file, defaulting to '
            'the current version. Returns document/version IDs, same (boolean), and method; '
            'never prints contents or checksums. New versions use encrypted-sha256 without '
            'downloading stored file chunks. Legacy versions use legacy-download, verifying '
            'and decrypting stored contents in memory without temporary files. Reads the local '
            'file internally; symlinks and files over 8 MiB are rejected. A metadata match does '
            'not verify stored chunk availability. Verify the writer with vaultcontext verify-user first.',
            'compare DOCUMENT_ID /path/to/file --version VERSION_ID', 'document', 'path')
    q.add_argument('--version', metavar='VERSION_ID',
                   help='version ID from vaultcontext history (default: current version)')
    q = add('cat', 'Write exact file contents to stdout',
            unlocked + 'Write exact bytes, defaulting to the current version, after verifying '
            'the entire file. Verify the writer with vaultcontext verify-user first. No JSON, headings or '
            'added newline; errors go to stderr. Creates no plaintext temporary files and never '
            'executes contents. Output may contain secrets, binary data or terminal control '
            'characters. Shell redirection uses ordinary shell permissions and overwrite behavior; '
            'use vaultcontext restore for protected file creation.',
            'cat DOCUMENT_ID --version VERSION_ID', 'document')
    q.add_argument('--version', metavar='VERSION_ID',
                   help='version ID from vaultcontext history (default: current version)')
    q = add('restore', 'Restore a file version to a local path',
            unlocked + 'Restore exact bytes, defaulting to the current version. Verify the '
            'writer with vaultcontext verify-user first. Existing destinations require --overwrite. '
            'Parents must exist; symlink destinations and ancestors are rejected. Writes mode '
            '0600; original permissions and executable bits are not restored. Never executes the file.',
            'restore DOCUMENT_ID --to /path/to/destination', 'document')
    q.add_argument('--version', metavar='VERSION_ID',
                   help='version ID from vaultcontext history (default: current version)')
    destination(q)
    add('directory', 'List registered accounts and public keys',
        'Requires login; no unlock needed. List user names, emails, account IDs and public keys. '
        'Directory values are not independent proof of identity; verify fingerprints through a trusted channel.',
        'directory')
    q = add('verify-user', 'Pin an independently verified fingerprint',
            'Requires login; no unlock needed. Compare the supplied fingerprint with the account\'s '
            'public keys and pin it locally for this server and user. Verify owners and writers '
            'before reading their signed data; verify recipients before sharing.',
            'verify-user ACCOUNT_ID --fingerprint VERIFIED_FINGERPRINT', 'account')
    fingerprint(q, 'account')
    add('members', 'List active vault members and roles',
        unlocked + 'List active owner, editor and reader memberships with account IDs.', 'members VAULT_ID', 'vault')
    q = add('share', 'Invite a reader or editor to the whole vault',
            unlocked + 'Owner only. Verify the recipient fingerprint independently. Acceptance '
            'grants the whole vault and all retained history. Use a dedicated vault for a single-file '
            'share. To change an active member\'s role, revoke/rotate and then invite again.',
            'share VAULT_ID ACCOUNT_ID --role reader --fingerprint VERIFIED_RECIPIENT_FINGERPRINT',
            'vault', 'account')
    q.add_argument('--role', choices=['reader', 'editor'], default='reader',
                   help='reader can restore; editor can also save (default: %(default)s)')
    fingerprint(q, 'recipient')
    add('invitations', 'List accessible pending invitations',
        unlocked + 'List pending invitations and their IDs. Use vaultcontext accept for an invitation addressed to you.',
        'invitations')
    q = add('accept', 'Accept a vault invitation',
            unlocked + 'Verify the owner\'s fingerprint independently, authenticate the offered '
            'key history, then accept your invitation.',
            'accept INVITATION_ID --fingerprint VERIFIED_OWNER_FINGERPRINT', 'invitation')
    fingerprint(q, 'owner')
    add('revoke', 'Remove a member and rotate future-write keys',
        unlocked + 'Owner only; owners cannot remove themselves. Revoke the member, cancel pending '
        'invitations and freeze writes before rotating keys for remaining members. Previously '
        'downloaded plaintext and keys cannot be recalled; rotation protects future versions. '
        'If rotation fails, verify remaining members and run vaultcontext rotate. Reissue canceled invitations afterward.',
        'revoke VAULT_ID ACCOUNT_ID', 'vault', 'account')
    add('rotate', 'Rotate vault keys or finish a failed revocation',
        unlocked + 'Owner only. Independently verify and pin all remaining member fingerprints '
        'with vaultcontext verify-user first. Publish keys for a new generation and unfreeze writes after '
        'revocation. Existing versions keep their original keys; reissue canceled invitations afterward.',
        'rotate VAULT_ID', 'vault')
    q = add('export', 'Export retained files to an encrypted archive',
            unlocked + 'Export all accessible retained file versions. Prompts twice for a separate '
            'archive passphrase in an interactive terminal. Includes archived documents and their status. '
            'Contains no identity private keys or '
            'live vault-key envelopes and cannot recover your live-vault passphrase. The plaintext '
            'archive limit is 64 MiB, including base64 and metadata. Restore without a server using '
            'vaultcontext restore-export. Parent directory must exist; output mode is 0600.',
            'export VAULT_ID --to /path/to/files.vault-export', 'vault')
    destination(q)
    add('inspect-export', 'List document and version IDs in an archive',
        'Works offline without server configuration, login or unlock. Prompts for the archive '
        'passphrase in an interactive terminal and decrypts in memory. Lists metadata without '
        'file contents; use its document/version IDs with vaultcontext restore-export.',
        'inspect-export /path/to/files.vault-export', 'archive')
    q = add('restore-export', 'Restore one file from an encrypted archive',
            'Works offline without server configuration, login or unlock. Prompts for the archive '
            'passphrase in an interactive terminal. Decrypts in memory and writes only the selected '
            'file, with mode 0600. Parent directory must exist; symlink destinations and ancestors '
            'are rejected. Refuses existing destinations unless --overwrite is supplied. Never executes the file.',
            'restore-export /path/to/files.vault-export --document DOCUMENT_ID --version VERSION_ID --to /path/to/file',
            'archive')
    q.add_argument('--document', required=True, metavar='DOCUMENT_ID', help='document ID from vaultcontext inspect-export')
    q.add_argument('--version', required=True, metavar='VERSION_ID', help='version ID from vaultcontext inspect-export')
    destination(q)

    groups = [
        ('Account and keys', ('login', 'whoami', 'logout', 'check', 'init', 'unlock', 'lock', 'change-passphrase', 'keychain-enroll', 'keychain-forget')),
        ('Vaults and files', ('vaults', 'create', 'save', 'list', 'search', 'history', 'compare', 'cat', 'restore', 'archive', 'unarchive')),
        ('Sharing', ('directory', 'verify-user', 'members', 'share', 'invitations', 'accept', 'revoke', 'rotate')),
        ('Encrypted archives', ('export', 'inspect-export', 'restore-export')),
    ]
    p.epilog = '\n\n'.join(
        title + ':\n' + '\n'.join(f'  {name:19} {summaries[name]}' for name in names)
        for title, names in groups) + '\n\n' + p.epilog
    return p

def run(args):
    if args.command in ('restore-export', 'inspect-export'):
        archive = json.loads(crypto.read_archive(args.archive))
        data = crypto.parse_export(crypto.decrypt_export(archive, prompt_passphrase()))
        if args.command == 'inspect-export':
            return [{k: v for k, v in f.items() if k != 'data'} for f in data['files']]
        matches = [f for f in data['files'] if f['document'] == args.document and f['version'] == args.version]
        if len(matches) != 1:
            raise auth.Fail(1, 'Export does not contain that document/version.')
        crypto.restore_file(args.to, base64.b64decode(matches[0]['data'], validate=True), overwrite=args.overwrite)
        return {'restored': args.version}
    cfg = auth.config()
    if args.command not in ('lock', 'logout', 'keychain-forget'):
        demo.configure(cfg, args.command)
    if args.command == 'login':
        if cfg.get('_demo_generation'):
            session.stop(cfg)
        auth.google_login(cfg, args.port, args.timeout)
        return {'signed_in': True}
    if args.command == 'logout':
        return session.stop(cfg, logout=True)
    if args.command == 'lock':
        return session.stop(cfg)
    if args.command == 'whoami':
        return {'id': whoami(cfg)}
    if args.command == 'check':
        schema = request(cfg, 'GET', '/api/context/schema')
        expected = json.loads(auth.SCHEMA_FILE.read_text())
        actual = {t['name']: sorted(c['name'] for c in t['columns']) for t in schema['tables']}
        if actual != expected:
            raise auth.Fail(3, 'Server schema differs from the bundled schema snapshot.')
        return {'compatible': True}
    if args.command == 'directory':
        return {'users': rows(cfg, 'user_directory'), 'keys': rows(cfg, 'identities')}
    if args.command == 'verify-user':
        verify_user(cfg, args.account, args.fingerprint)
        return {'verified': args.account}
    if args.command == 'init':
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        account = whoami(cfg)
        # Verify read access/enrollment and existing identity before asking for a
        # secret or generating replacement keys. The write still handles races.
        if rows(cfg, 'identities', 'account=' + quote(account)):
            raise auth.Fail(4, 'Identity already initialized. Run vaultcontext unlock with your existing passphrase. init does not replace an identity or reset its passphrase.')
        identity = crypto.generate_identity()
        bundle = crypto.wrap_identity(identity, prompt_passphrase(confirm=True), account)
        pub = crypto.public_identity(identity)
        fingerprint = crypto.fingerprint(pub)
        result = action(cfg, 'identity_init', {'public_key': pub['enc_public'], 'signing_key': pub['sign_public'], 'fingerprint': fingerprint, 'key_bundle': encode(bundle)})
        verify_user(cfg, account, fingerprint)
        return dict(result, fingerprint=fingerprint)
    if args.command == 'unlock':
        return unlock(cfg, args.timeout, use_keychain=args.keychain)
    if args.command == 'keychain-enroll':
        return keychain_enroll(cfg)
    if args.command == 'keychain-forget':
        keychain.require_helper()
        keychain.call(cfg, whoami(cfg), 'delete')
        return {'keychain_forgotten': True}
    payload = vars(args).copy()
    if args.command == 'save' and payload.get('name') is None:
        payload['name'] = payload['path']
    for key in ('path', 'to'):
        if payload.get(key):
            payload[key] = os.path.abspath(payload[key])
    if args.command == 'change-passphrase':
        payload['new_passphrase'] = prompt_passphrase(confirm=True)
    if args.command == 'export':
        payload['export_passphrase'] = prompt_passphrase(confirm=True)
    result = session_call(cfg, payload)
    if args.command == 'cat':
        return base64.b64decode(result['data'], validate=True)
    return result

def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        command_parser = parser()
        if len(sys.argv) == 1:
            command_parser.print_help()
            return 0
        args = command_parser.parse_args()
        result = run(args)
        if args.command == 'cat':
            sys.stdout.buffer.write(result)
            sys.stdout.buffer.flush()
        else:
            print(encode(result))
    except BrokenPipeError:
        # Prevent another flush of a closed pipe during interpreter shutdown.
        with open(os.devnull, 'wb') as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 1
    except auth.Fail as error:
        auth.say(str(error)); return error.code
    except FileNotFoundError:
        auth.say('Required file or unlock session missing; check paths or run unlock.'); return 1
    except KeyboardInterrupt:
        auth.say('Cancelled.'); return 1
    except Exception:
        auth.say('Vault operation failed; sensitive error details suppressed.'); return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
