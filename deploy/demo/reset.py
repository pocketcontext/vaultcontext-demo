#!/usr/bin/env python3
"""Destructive, fenced reset of one explicitly bound demo deployment only."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit


class ResetError(RuntimeError):
    pass


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ResetError('absolute normalized paths required')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ResetError('symlink paths are forbidden')
    return path


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def configuration(path):
    path = safe_path(Path(path).absolute())
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ResetError('configuration must be a private owned regular file')
    data = json.loads(path.read_text())
    if set(data) != {'deployment', 'origin', 'root', 'storage', 'hooks'} or data['deployment'] != 'vaultcontext-demo':
        raise ResetError('invalid demo deployment binding')
    root = safe_path(data['root'])
    if root.name != 'vaultcontext-demo' or not root.is_dir():
        raise ResetError('root must be an existing dedicated vaultcontext-demo directory')
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ResetError('demo root must be private and owned')
    origin = urlsplit(data['origin'])
    local = data['storage'].get('kind') == 'local'
    if origin.path or origin.query or origin.fragment or origin.username or origin.password:
        raise ResetError('invalid origin')
    if local:
        if origin.scheme != 'http' or origin.hostname not in ('127.0.0.1', 'localhost'):
            raise ResetError('local adapter requires a loopback origin')
    elif origin.scheme != 'https' or not origin.hostname or not re.fullmatch(r'(vault-demo|demo-vault|vaultcontext-demo)\.[a-z0-9.-]+', origin.hostname):
        raise ResetError('remote reset requires an explicitly named demo hostname')
    if set(data['hooks']) != {'fence', 'stop', 'assert_stopped', 'initialize', 'start', 'health', 'unfence'}:
        raise ResetError('all lifecycle hooks are required')
    for command in data['hooks'].values():
        if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command) or not command[0].startswith('/'):
            raise ResetError('hooks require absolute executable argument arrays')
    if local:
        if set(data['storage']) != {'kind'}:
            raise ResetError('unexpected local storage settings')
    else:
        if set(data['storage']) != {'kind', 'primary_bucket', 'replica_bucket', 'durable_bucket'} or data['storage']['kind'] not in ('s3', 'r2'):
            raise ResetError('invalid remote storage settings')
        for field, chosen in (('primary_bucket', 'once-v2-vaultcontext-demo-files'),
                              ('replica_bucket', 'once-v2-vaultcontext-demo-replica')):
            if data['storage'][field] != chosen and not re.fullmatch(r'vaultcontext-demo-[a-z0-9][a-z0-9-]{1,40}', data['storage'][field]):
                raise ResetError('only dedicated vaultcontext-demo buckets are accepted')
        if data['storage']['durable_bucket'] != 'once-v2-vaultcontext-demo-contacts-replica' and not re.fullmatch(r'vaultcontext-demo-contacts-[a-z0-9][a-z0-9-]{1,32}',data['storage']['durable_bucket']):
            raise ResetError('durable contact exclusion binding is required')
        if len({data['storage'][key] for key in ('primary_bucket','replica_bucket','durable_bucket')})!=3:
            raise ResetError('reset storage may never overlap durable contact storage')
        if data['storage']['primary_bucket'] == data['storage']['replica_bucket']:
            raise ResetError('primary and replica buckets must differ')
    return data


class S3Bucket:
    def __init__(self, bucket, prefix, client=None, kind='s3'):
        self.bucket = bucket
        self.kind = kind
        if client is not None:
            self.client = client
            return
        import boto3
        from botocore.config import Config
        def setting(name):
            value = os.environ.get(prefix + name, '')
            if not value:
                raise ResetError('incomplete dedicated storage credentials')
            return value
        if setting('BUCKET') != bucket:
            raise ResetError('configured bucket differs from reset allowlist')
        endpoint = setting('ENDPOINT')
        if kind == 'r2' and not re.fullmatch(r'https://[a-f0-9]{32}(\.eu|\.fedramp)?\.r2\.cloudflarestorage\.com', endpoint):
            raise ResetError('R2 adapter requires an exact Cloudflare account endpoint')
        if not endpoint.startswith('https://'):
            raise ResetError('remote storage requires HTTPS')
        self.client = boto3.client('s3', endpoint_url=endpoint, region_name=setting('REGION'),
            aws_access_key_id=setting('ACCESS_KEY_ID'), aws_secret_access_key=setting('SECRET_ACCESS_KEY'),
            config=Config(connect_timeout=10, read_timeout=30, retries={'max_attempts': 3}))

    def erase(self):
        # Enumerate every generation, including delete markers and unfinished uploads.
        # Unsupported APIs fail closed; no assumption that versioning is disabled.
        if self.kind != 'r2':
            for page in self.client.get_paginator('list_object_versions').paginate(Bucket=self.bucket):
                objects = [{'Key': v['Key'], 'VersionId': v['VersionId']}
                           for v in page.get('Versions', []) + page.get('DeleteMarkers', [])]
                self.delete(objects)
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket):
            self.delete([{'Key': item['Key']} for item in page.get('Contents', [])])
        for page in self.client.get_paginator('list_multipart_uploads').paginate(Bucket=self.bucket):
            for upload in page.get('Uploads', []):
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=upload['Key'], UploadId=upload['UploadId'])
        for operation, keys in (('list_object_versions', ('Versions', 'DeleteMarkers')),
                                ('list_objects_v2', ('Contents',)), ('list_multipart_uploads', ('Uploads',))):
            if self.kind == 'r2' and operation == 'list_object_versions':
                continue  # R2 has no object-versioning API; explicit provider-only exception.
            for page in self.client.get_paginator(operation).paginate(Bucket=self.bucket):
                if any(page.get(key) for key in keys):
                    raise ResetError('remote deletion is incomplete')

    def delete(self, objects):
        for offset in range(0, len(objects), 1000):
            result = self.client.delete_objects(Bucket=self.bucket, Delete={'Objects': objects[offset:offset+1000], 'Quiet': True})
            if result.get('Errors'):
                raise ResetError('remote deletion failed')


def hook(config, name, generation):
    env = {key:value for key,value in os.environ.items() if not key.startswith('CONTACTS_LITESTREAM_')}
    env.update(VAULTCONTEXT_DEMO_MODE='true', VAULTCONTEXT_DEMO_GENERATION=generation,
               VAULTCONTEXT_DEMO_RESET_ROOT=config['root'], VAULTCONTEXT_DEMO_RESET_PHASE=name,
               VAULTCONTEXT_DEMO_RESET_FENCE=str(Path(config['root']) / 'control/reset-pending.json'))
    try:
        result = subprocess.run(config['hooks'][name], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    except Exception:
        raise ResetError('lifecycle hook failed: ' + name) from None
    if result.returncode:
        raise ResetError('lifecycle hook failed: ' + name)


def reset(config, generation, dry_run=False, clients=None):
    try:
        if datetime.strptime(generation, '%Y-%m-%d').strftime('%Y-%m-%d') != generation:
            raise ValueError()
    except ValueError:
        raise ResetError('invalid generation') from None
    root = safe_path(config['root'])
    control = safe_path(root / 'control')
    control.mkdir(mode=0o700, exist_ok=True)
    if (control/'migration-fenced.json').exists():
        raise ResetError('host migration fence prohibits daily reset')
    for item in ('runtime', 'persistent'):
        safe_path(root / item)
    if dry_run:
        return {'dryRun': True, 'generation': generation, 'storage': config['storage']['kind']}
    lock_fd = os.open(control / 'reset.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResetError('another reset owns the lifecycle lock') from None
        if (control/'migration-fenced.json').exists():
            raise ResetError('host migration fence prohibits daily reset')
        pending = control / 'reset-pending.json'
        complete = control / 'generation.json'
        for item in (pending, complete):
            safe_path(item)
        if not pending.exists() and complete.exists() and json.loads(complete.read_text()).get('generation') == generation:
            return {'alreadyComplete': True, 'generation': generation}
        if pending.exists():
            state = json.loads(pending.read_text())
            if state.get('deployment') != 'vaultcontext-demo' or state.get('generation') != generation:
                raise ResetError('pending reset must be resumed with its original generation')
        else:
            state = {'deployment': 'vaultcontext-demo', 'generation': generation, 'phase': 'pending'}
            atomic_json(pending, state)
        if (control/'app-state').is_dir():atomic_json(control/'app-state/reset-pending.json',state)
        def phase(value):
            state['phase'] = value
            atomic_json(pending, state)
            if (control/'app-state').is_dir():atomic_json(control/'app-state/reset-pending.json',state)
        try:
            # On every retry re-establish fencing and prove all writers stopped.
            hook(config, 'fence', generation)
            hook(config, 'stop', generation)
            hook(config, 'assert_stopped', generation)
            phase('stopped')
            if config['storage']['kind'] in ('s3', 'r2'):
                if clients is None and os.environ.get('VAULTCONTEXT_S3_ACCESS_KEY_ID') == os.environ.get('LITESTREAM_ACCESS_KEY_ID'):
                    raise ResetError('primary and replica credentials must differ')
                buckets = [S3Bucket(config['storage'][field], prefix, None if clients is None else clients[index], config['storage']['kind'])
                           for index, (field, prefix) in enumerate((('primary_bucket', 'VAULTCONTEXT_S3_'), ('replica_bucket', 'LITESTREAM_')))]
                for bucket in buckets:
                    bucket.erase()
            runtime = safe_path(root / 'runtime')
            if runtime.exists():
                if not runtime.is_dir():
                    raise ResetError('runtime must be a directory')
                # Includes main+auxiliary DBs, journals, local files, replica generations and caches.
                shutil.rmtree(runtime)
            runtime.mkdir(mode=0o700)
            phase('purged')
            hook(config, 'initialize', generation)
            phase('initialized')
            hook(config, 'start', generation)
            hook(config, 'health', generation)
            phase('healthy')
            atomic_json(complete, {'generation': generation})
            # Ingress remains fenced until healthy fresh state is confirmed.
            hook(config, 'unfence', generation)
            (control/'app-state/reset-pending.json').unlink(missing_ok=True)
            if (control/'app-state').is_dir():
                descriptor=os.open(control/'app-state',os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            pending.unlink()
            descriptor=os.open(control,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
            return {'reset': True, 'generation': generation}
        except Exception:
            # Do not resume traffic or delete the durable marker after any error.
            # Re-fence if an adapter partially lifted ingress before failing.
            try:
                hook(config, 'fence', generation)
                hook(config, 'stop', generation)
            except Exception:
                pass
            raise ResetError('reset failed; durable fence retained; operator must investigate') from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--generation', default=datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = reset(configuration(args.config), args.generation, args.dry_run)
        print(json.dumps(result))
    except Exception:
        print('demo reset failed; configuration or reset fence needs operator review', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
