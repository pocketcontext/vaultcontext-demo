#!/usr/bin/python3 -I
"""S3 evidence verification and Litestream startup; no archive backup or supervisor.

The default command requires an existing database or a recoverable replica.
`init` is a one-shot fresh-install operation; `serve` is Litestream's child.
Python replaces itself with Litestream/PocketContext once preparation is done.
"""
import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile

APP = Path('/app')
# Must match the database path in /etc/litestream.yml.
ONCE_CHILD = os.environ.get('VAULTCONTEXT_DEMO_ONCE') == 'true' and os.environ.get('VAULTCONTEXT_DEMO_ONCE_CHILD') == 'app'
DATA = Path(os.environ.get('VAULTCONTEXT_DEMO_ONCE_ROOT', '/storage')) / 'runtime/pb_data' if ONCE_CHILD else Path('/storage/pb_data')
SERVER = '/usr/local/bin/pocketcontext'
LITESTREAM = '/usr/local/bin/litestream'
SELF = '/usr/local/bin/vaultcontext-entrypoint.py'
CONFIG = '/etc/litestream.yml'
DEMO_CONFIG = '/run/vaultcontext-demo-litestream.yml'
SOCKET = '/run/litestream.sock'
S3_FIELDS = {'bucket': 'BUCKET', 'endpoint': 'ENDPOINT', 'region': 'REGION',
             'accessKey': 'ACCESS_KEY_ID', 'secret': 'SECRET_ACCESS_KEY'}


class StartupError(RuntimeError):
    """Only fixed, non-secret messages may be included."""


def log(message):
    print('entrypoint: ' + message, flush=True)


def demo_fence(mode):
    """A reset marker outside disposable storage prevents accidental restarts."""
    if os.environ.get('VAULTCONTEXT_DEMO_MODE') != 'true':
        return
    value = os.environ.get('VAULTCONTEXT_DEMO_RESET_FENCE', '')
    marker = Path(value)
    if not value or not marker.is_absolute() or '..' in marker.parts or marker == DATA or DATA in marker.parents:
        raise StartupError('demo requires an external absolute reset fence')
    for part in (marker, *marker.parents):
        if part.is_symlink():
            raise StartupError('demo reset fence cannot traverse symlinks')
    migration = marker.parent / 'migration-fenced.json'
    if migration.exists() or migration.is_symlink():
        state = private_json(migration)
        if (set(state) != {'deployment','generation','phase'} or state.get('deployment') != 'vaultcontext-demo'
                or state.get('generation') != os.environ.get('VAULTCONTEXT_DEMO_GENERATION')):
            raise StartupError('invalid migration fence')
        expected = {'handoff': ('handoff','source-fenced'),
                    'adopt-handoff': ('adopt-handoff','target-restoring'),
                    'start': ('migrate-start','target-verified'), 'serve': ('migrate-start','target-verified'),
                    'verify': ('migrate-start','target-verified')}.get(mode)
        if not expected or (os.environ.get('VAULTCONTEXT_DEMO_RESET_PHASE'),state.get('phase')) != expected:
            raise StartupError('host migration is fenced; ordinary startup is forbidden')
    elif mode in ('handoff','adopt-handoff'):
        raise StartupError('host migration requires its explicit durable fence')
    try:
        info = marker.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
        raise StartupError('unsafe demo reset fence')
    try:
        state = json.loads(marker.read_text())
    except (OSError, ValueError):
        raise StartupError('invalid demo reset fence') from None
    if mode in ('handoff','adopt-handoff'):
        raise StartupError('daily reset and host migration cannot overlap')
    allowed = {'init': ('initialize', 'purged'), 'start': ('start', 'initialized'),
               'serve': ('start', 'initialized'), 'verify': ('health', 'initialized')}
    phase, stored = allowed[mode]
    if (state.get('deployment') != 'vaultcontext-demo' or
            state.get('generation') != os.environ.get('VAULTCONTEXT_DEMO_GENERATION') or
            os.environ.get('VAULTCONTEXT_DEMO_RESET_PHASE') != phase or state.get('phase') != stored):
        raise StartupError('demo reset is pending; ordinary startup is fenced')


def private_json(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise StartupError('private control state cannot traverse symlinks')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
        raise StartupError('invalid private control state')
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise StartupError('invalid private control state') from None
    if not isinstance(value, dict):
        raise StartupError('invalid private control state')
    return value


def write_private_json(path, value):
    if path.is_symlink():
        raise StartupError('control state cannot replace a symlink')
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.demo-handoff-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary,path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_config():
    if os.environ.get('VAULTCONTEXT_DATA_DIR', '/storage/pb_data') != '/storage/pb_data':
        raise StartupError('VAULTCONTEXT_DATA_DIR must be /storage/pb_data to match Litestream')
    if os.environ.get('LITESTREAM_DISABLED'):
        raise StartupError('LITESTREAM_DISABLED is unsupported; replication is required')
    for prefix, names in (
        ('VAULTCONTEXT_S3_', S3_FIELDS.values()),
        ('LITESTREAM_', ('BUCKET', 'PATH', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY')),
    ):
        missing = [prefix + name for name in names if not os.environ.get(prefix + name, '').strip()]
        if missing:
            raise StartupError('missing required configuration: ' + ', '.join(missing))
    if os.environ.get('VAULTCONTEXT_S3_FORCE_PATH_STYLE', 'true') not in ('true', 'false'):
        raise StartupError('invalid object storage path style')
    for left, right in (('VAULTCONTEXT_GOOGLE_CLIENT_ID', 'VAULTCONTEXT_GOOGLE_CLIENT_SECRET'),
                        ('VAULTCONTEXT_SUPERUSER_EMAIL', 'VAULTCONTEXT_SUPERUSER_PASSWORD')):
        if bool(os.environ.get(left)) != bool(os.environ.get(right)):
            raise StartupError(left + ' and ' + right + ' must be set together')
    if os.environ['VAULTCONTEXT_S3_BUCKET'].strip() == os.environ['LITESTREAM_BUCKET'].strip():
        raise StartupError('primary files and database replicas require separate buckets')
    if os.environ['VAULTCONTEXT_S3_ACCESS_KEY_ID'].strip() == os.environ['LITESTREAM_ACCESS_KEY_ID'].strip():
        raise StartupError('primary files and database replicas require separate credentials')
    for key, default in (('LITESTREAM_REGION', ''), ('LITESTREAM_ENDPOINT', ''),
                         ('LITESTREAM_SYNC_INTERVAL', '10s')):
        os.environ.setdefault(key, default)


def demo_mode():
    return os.environ.get('VAULTCONTEXT_DEMO_MODE') == 'true'


def replication_config():
    return DEMO_CONFIG if demo_mode() else CONFIG


def configure_replication():
    """Keep production's single-DB template; demo appends its auxiliary replica.

    The generated file contains environment references, never credentials. Each
    database has an independent replica path; daily reset clears the whole bucket.
    """
    if not demo_mode():
        return
    prefix = os.environ.get('LITESTREAM_PATH', '')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', prefix) or any(part in ('', '.', '..') for part in prefix.split('/')):
        raise StartupError('demo replica path must be a normalized private prefix')
    os.environ['LITESTREAM_DEMO_AUXILIARY_PATH'] = prefix + '/auxiliary'
    addition = '\n'.join([
        '', '  - path: ' + str(DATA / 'auxiliary.db'), '    replica:', '      type: s3',
        '      bucket: "${LITESTREAM_BUCKET}"', '      path: "${LITESTREAM_DEMO_AUXILIARY_PATH}"',
        '      region: "${LITESTREAM_REGION}"', '      endpoint: "${LITESTREAM_ENDPOINT}"',
        '      sync-interval: "${LITESTREAM_SYNC_INTERVAL}"', '',
    ])
    target = Path(DEMO_CONFIG)
    for part in (target, *target.parents):
        if part.is_symlink():
            raise StartupError('demo replica configuration cannot traverse symlinks')
    with tempfile.NamedTemporaryFile(mode='w', prefix='.demo-litestream-', dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            template = Path(CONFIG).read_text()
            if ONCE_CHILD:
                template = template.replace('/storage/pb_data/data.db', str(DATA / 'data.db'))
            stream.write(template + addition)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def verify_auxiliary(data):
    auxiliary = data / 'auxiliary.db'
    if auxiliary.is_symlink() or not auxiliary.is_file():
        raise StartupError('a complete auxiliary database is required; refusing to recreate it')
    with closing(sqlite3.connect(auxiliary.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise StartupError('auxiliary database integrity check failed')
        if demo_mode() and not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='_logs'").fetchone():
            raise StartupError('auxiliary database is missing its expected schema')


def verify_demo_generation(data):
    if not demo_mode():
        return
    generation = os.environ.get('VAULTCONTEXT_DEMO_GENERATION', '')
    if generation != datetime.now(timezone.utc).date().isoformat():
        raise StartupError('demo generation expired; restore cannot extend the daily deadline')
    with closing(database_connection(data)) as conn:
        row = conn.execute('SELECT enabled,generation FROM demo_policy WHERE id=?', ('demopolicy00001',)).fetchone()
    if row != (1, generation):
        raise StartupError('restored demo generation differs from the current daily generation')


def maintenance_state(data):
    marker = data / 'maintenance.json'
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
        raise StartupError('invalid maintenance state; startup stopped')
    try:
        state = json.loads(marker.read_text())
    except (ValueError, OSError):
        raise StartupError('invalid maintenance state; startup stopped') from None
    if (not isinstance(state, dict) or set(state) != {'readOnly', 'generation'}
            or type(state.get('readOnly')) is not bool
            or type(state.get('generation')) is not int
            or not 0 <= state['generation'] <= 18446744073709551615):
        raise StartupError('invalid maintenance state; startup stopped')
    return state['readOnly']


def database_connection(data):
    db = data / 'data.db'
    if db.is_symlink() or not db.is_file():
        raise StartupError('a regular database file is required')
    return sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)


def identifier(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise StartupError('invalid collection metadata')
    return '"' + value.replace('"', '""') + '"'


def path_part(value):
    if (not isinstance(value, str) or not value or value in ('.', '..')
            or '/' in value or '\\' in value or '\x00' in value):
        raise StartupError('unsafe original path')
    return value


def refs(data):
    """Inventory actual file fields, including avatars; ciphertext has a stored hash."""
    with closing(database_connection(data)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise StartupError('database integrity check failed')
        collections = conn.execute('SELECT id,name,type,fields FROM _collections').fetchall()
        chunks = next((c for c in collections if c[1] == 'version_chunks' and c[2] == 'base'), None)
        if chunks is None:
            raise StartupError('database has no VaultContext ciphertext schema')
        result = {}
        for collection, table, kind, raw_fields in collections:
            if kind == 'view':
                continue  # Views refer to their underlying records, not independent objects.
            if kind not in ('base', 'auth'):
                raise StartupError('unsupported collection type')
            fields = json.loads(raw_fields)
            if not isinstance(fields, list) or any(not isinstance(f, dict) for f in fields):
                raise StartupError('invalid collection fields')
            for field in fields:
                if field.get('type') != 'file':
                    continue
                column = field.get('name')
                multiple = field.get('maxSelect', 1)
                if type(multiple) is not int or multiple < 1:
                    raise StartupError('invalid file cardinality')
                query = 'SELECT id,' + identifier(column) + ' FROM ' + identifier(table)
                for record, value in conn.execute(query):
                    if value is None or value == '':
                        continue
                    filenames = json.loads(value) if multiple > 1 else [value]
                    if not isinstance(filenames, list):
                        raise StartupError('invalid file reference list')
                    for filename in filenames:
                        key = '/'.join(path_part(v) for v in (collection, record, filename))
                        result[key] = None
        # Every ciphertext row must have exactly one valid immutable object and SHA-256.
        for record, filename, sha in conn.execute('SELECT id,ciphertext,sha256 FROM version_chunks'):
            key = '/'.join(path_part(v) for v in (chunks[0], record, filename))
            if key not in result:
                raise StartupError('ciphertext field missing from collection inventory')
            if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                raise StartupError('ciphertext has no valid checksum')
            result[key] = sha
        return result


def stored_storage_settings(data):
    with closing(database_connection(data)) as conn:
        row = conn.execute("SELECT value FROM _params WHERE id='settings'").fetchone()
    if not row:
        raise StartupError('missing persisted storage settings')
    try:
        settings = json.loads(row[0]).get('s3')
        if not isinstance(settings, dict):
            raise ValueError()
        return settings
    except (ValueError, AttributeError):
        raise StartupError('cannot determine persisted storage mode') from None


def verify_frozen_storage(data):
    if not maintenance_state(data):
        return
    desired = {field: os.environ.get('VAULTCONTEXT_S3_' + name, '').strip()
               for field, name in S3_FIELDS.items()}
    desired.update(enabled=True, forcePathStyle=os.environ.get('VAULTCONTEXT_S3_FORCE_PATH_STYLE', 'true') == 'true')
    current = stored_storage_settings(data)
    if any(current.get(field) != value for field, value in desired.items()):
        raise StartupError('frozen object storage configuration differs from stored settings; startup refused')


def verify_remote(data, client=None):
    config = {name: os.environ.get('VAULTCONTEXT_S3_' + name, '').strip() for name in S3_FIELDS.values()}
    if not all(config.values()):
        raise StartupError('incomplete object storage configuration')
    if client is None:
        import boto3
        from botocore.config import Config
        endpoint = config['ENDPOINT']
        if '://' not in endpoint:
            endpoint = 'https://' + endpoint
        client = boto3.client('s3', endpoint_url=endpoint, region_name=config['REGION'],
            aws_access_key_id=config['ACCESS_KEY_ID'], aws_secret_access_key=config['SECRET_ACCESS_KEY'],
            config=Config(connect_timeout=10, read_timeout=60, retries={'max_attempts': 3},
                s3={'addressing_style': 'path' if os.environ.get('VAULTCONTEXT_S3_FORCE_PATH_STYLE', 'true') == 'true' else 'virtual'}))
    for name, expected in refs(data).items():
        response = client.get_object(Bucket=config['BUCKET'], Key=name)
        with closing(response['Body']) as body:
            checksum = hashlib.sha256()
            received = 0
            for chunk in iter(lambda: body.read(1024 * 1024), b''):
                checksum.update(chunk)
                received += len(chunk)
        if 'ContentLength' in response and response['ContentLength'] != received:
            raise StartupError('incomplete remote original; startup refused')
        if expected is not None and checksum.hexdigest() != expected:
            raise StartupError('missing or corrupt remote original; startup refused')


def verify(data):
    if demo_mode():
        verify_auxiliary(data)
        verify_demo_generation(data)
    verify_frozen_storage(data)
    verify_remote(data)


def app_flags(data):
    return ['--dir=' + str(data), '--migrationsDir=' + str(APP / 'pb_migrations'),
            '--hooksDir=' + str(APP / 'pb_hooks'), '--contextConfig=' + str(APP / 'pocketcontext.json')]


def run_command(args, env=None, timeout=None):
    """Forward termination during preparation; never expose child output/secrets."""
    child = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    def interrupted(signum, frame):
        if child.poll() is None:
            os.killpg(child.pid, signum)
        raise SystemExit(128 + signum)
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        if child.wait(timeout=timeout) != 0:
            raise StartupError('startup command failed; refusing to serve')
    finally:
        try:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def restore_database(data, initialize=False):
    names = ('data.db', 'auxiliary.db') if demo_mode() else ('data.db',)
    db = data / 'data.db'
    marker = data / 'restoration.pending'
    if marker.exists() or marker.is_symlink():
        raise StartupError('incomplete paired restore; recover into a fresh volume')
    if db.exists() or db.is_symlink():
        if initialize:
            raise StartupError('init requires an empty database directory')
        if not db.is_file() or db.is_symlink():
            raise StartupError('a regular database file is required')
        if demo_mode():
            verify_auxiliary(data)
        log('database exists in the volume: no restore')
        return
    if any(data.iterdir()):
        raise StartupError('database missing but local state remains; operator recovery required')
    # Stage both demo databases before installing either. Partial installation
    # leaves a durable marker so a later process cannot recreate missing state.
    with tempfile.TemporaryDirectory(prefix='.vaultcontext-restore-', dir=data.parent) as temp:
        staged = Path(temp)
        for name in names:
            command = [LITESTREAM, 'restore', '-config', replication_config(), '-o', str(staged / name),
                       '-integrity-check', 'quick']
            if initialize:
                command.append('-if-replica-exists')
            command.append(str(data / name))
            try:
                run_command(command)
            except StartupError:
                raise StartupError('Litestream restore failed; check replica configuration and availability') from None
            if initialize and (staged / name).exists():
                raise StartupError('init refused: replica already exists; use normal startup to restore it')
        if initialize:
            return
        verify(staged)
        for name in names:
            with (staged / name).open('rb') as restored:
                os.fsync(restored.fileno())
        if demo_mode():
            with marker.open('x') as pending:
                pending.write('Paired database restoration must finish before startup.\n')
                pending.flush()
                os.fsync(pending.fileno())
            sync_directory(data)
        for name in names:
            os.replace(staged / name, data / name)
        sync_directory(data)
        if demo_mode():
            marker.unlink()
            sync_directory(data)
        log('databases restored and remote originals verified')
        return True


def database_digest(path):
    """Digest schema and all domain rows without emitting any record contents.

    Litestream's own coordination tables change during snapshots and are not
    application state. SQLite internal sequence/statistics tables remain covered.
    """
    if path.is_symlink() or not path.is_file():
        raise StartupError('regular handoff database required')
    digest = hashlib.sha256()
    def append(value):
        encoded = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
        digest.update(len(encoded).to_bytes(8,'big'));digest.update(encoded)
    def cell(value):
        if isinstance(value,bytes):
            return {'blob':base64.b64encode(value).decode('ascii')}
        return {'value':value}
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if db.execute('PRAGMA quick_check').fetchone()!=('ok',):
            raise StartupError('handoff database integrity failed')
        schema=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name NOT IN ('_litestream_seq','_litestream_lock') ORDER BY type,name").fetchall()
        append(schema)
        for kind,name,_,_ in schema:
            if kind!='table':
                continue
            columns=[row[1] for row in db.execute('PRAGMA table_info('+identifier(name)+')')]
            append([name,columns])
            ordered=','.join(identifier(column) for column in columns)
            for row in db.execute('SELECT * FROM '+identifier(name)+' ORDER BY '+ordered):
                append([cell(value) for value in row])
    return digest.hexdigest()


def manifest_generation(manifest):
    generation=os.environ.get('VAULTCONTEXT_DEMO_GENERATION','')
    if (set(manifest)!={'generation','databaseDigests','maintenance'} or
            manifest.get('generation')!=generation or generation!=datetime.now(timezone.utc).date().isoformat() or
            not isinstance(manifest.get('databaseDigests'),dict) or set(manifest['databaseDigests'])!={'data.db','auxiliary.db'} or
            any(not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value) for value in manifest['databaseDigests'].values())):
        raise StartupError('handoff manifest does not describe the current demo generation')
    maintenance=manifest['maintenance']
    if maintenance is not None and (not isinstance(maintenance,dict) or set(maintenance)!={'readOnly','generation'} or
            type(maintenance.get('readOnly')) is not bool or type(maintenance.get('generation')) is not int or
            not 0<=maintenance['generation']<=18446744073709551615):
        raise StartupError('invalid handoff maintenance state')


def handoff(data):
    if not demo_mode():
        raise StartupError('host migration commands are available only for the demo')
    validate_config();configure_replication();verify(data)
    if any((data/name).exists() or (data/name).is_symlink() for name in ('initialization.pending','restoration.pending')):
        raise StartupError('incomplete state cannot be handed off')
    before={name:database_digest(data/name) for name in ('data.db','auxiliary.db')}
    run_command([LITESTREAM,'replicate','-once','-force-snapshot','-config',replication_config()],timeout=240)
    with tempfile.TemporaryDirectory(prefix='.vaultcontext-handoff-',dir=data.parent) as directory:
        staged=Path(directory)
        for name in before:
            run_command([LITESTREAM,'restore','-config',replication_config(),'-o',str(staged/name),'-integrity-check','full',str(data/name)],timeout=120)
        verify(staged)
        if any(database_digest(staged/name)!=value or database_digest(data/name)!=value for name,value in before.items()):
            raise StartupError('handoff replica differs from stopped source; remain fenced')
    maintenance=private_json(data/'maintenance.json') if (data/'maintenance.json').exists() else None
    manifest={'generation':os.environ['VAULTCONTEXT_DEMO_GENERATION'],'databaseDigests':before,'maintenance':maintenance}
    manifest_generation(manifest)
    write_private_json(data.parent/'demo-handoff.json',manifest)
    log('both stopped demo databases replicated and verified for handoff')


def adopt_handoff(data):
    if not demo_mode():
        raise StartupError('host migration commands are available only for the demo')
    validate_config();configure_replication()
    control=Path(os.environ.get('VAULTCONTEXT_DEMO_RESET_FENCE','')).parent
    manifest=private_json(control/'incoming-runtime-handoff.json');manifest_generation(manifest)
    data.mkdir(mode=0o700,parents=True,exist_ok=True)
    if any(data.iterdir()):
        raise StartupError('handoff adoption requires an empty target database directory')
    restore_database(data)
    if any(database_digest(data/name)!=value for name,value in manifest['databaseDigests'].items()):
        raise StartupError('restored databases differ from source handoff; remain fenced')
    manifest_generation(manifest)  # Restoration cannot extend the daily deadline.
    if manifest['maintenance'] is not None:
        write_private_json(data/'maintenance.json',manifest['maintenance'])
    verify(data)
    write_private_json(data.parent/'demo-handoff.json',manifest)
    log('both demo databases adopted and verified without starting a writer')


def prepare(data, initialize=False):
    validate_config()
    configure_replication()
    frozen = maintenance_state(data)
    restored_marker = data / 'restoration.pending'
    if restored_marker.exists() or restored_marker.is_symlink():
        raise StartupError('incomplete paired restore; recover into a fresh volume')
    pending = data / 'initialization.pending'
    if pending.exists() or pending.is_symlink():
        raise StartupError('incomplete initialization; preserve this directory and recover into a fresh volume')
    if frozen and (initialize or not (data / 'data.db').is_file()):
        raise StartupError('frozen startup requires the existing database; initialization is forbidden')
    data.mkdir(parents=True, exist_ok=True)
    if initialize and any(data.iterdir()):
        raise StartupError('init requires an empty database directory')
    restored = False
    if not frozen:
        restored = restore_database(data, initialize)
    if initialize:
        # A failed/interrupted migrate or provisioning command must not turn into
        # an apparently healthy database on the next default startup.
        with pending.open('x') as marker:
            marker.write('Initialization must complete before normal startup.\n')
            marker.flush()
            os.fsync(marker.fileno())
        sync_directory(data)
        run_command([SERVER, 'migrate', 'up', *app_flags(data)])
    if frozen:
        verify_auxiliary(data)
    if not restored:
        verify(data)
    if frozen:
        log('read-only maintenance state: skipping superuser provisioning')
    elif os.environ.get('VAULTCONTEXT_SUPERUSER_EMAIL'):
        run_command([SERVER, 'superuser', 'upsert', *app_flags(data), '--',
                     os.environ['VAULTCONTEXT_SUPERUSER_EMAIL'], os.environ['VAULTCONTEXT_SUPERUSER_PASSWORD']])
    if initialize:
        pending.unlink()
        sync_directory(data)
        log('database initialized; run normal startup on this volume to begin replication')


def serve():
    validate_config()
    if demo_mode():
        verify_auxiliary(DATA)
        verify_demo_generation(DATA)
    try:
        names = ('data.db', 'auxiliary.db') if demo_mode() else ('data.db',)
        for name in names:
            run_command([LITESTREAM, 'sync', '-wait', '-timeout', '60', '-socket', SOCKET,
                         str(DATA / name)], timeout=65)
    except (StartupError, subprocess.TimeoutExpired):
        raise StartupError('initial replica synchronization failed; refusing to serve') from None
    if demo_mode():
        verify_demo_generation(DATA)  # Sync must not extend a generation across midnight.
    log('initial replica synchronization complete')
    env = dict(os.environ)
    for key in ('LITESTREAM_ACCESS_KEY_ID', 'LITESTREAM_SECRET_ACCESS_KEY',
                'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                'VAULTCONTEXT_SUPERUSER_PASSWORD'):
        env.pop(key, None)
    address = '127.0.0.1:8081' if ONCE_CHILD else '0.0.0.0:80'
    args = [SERVER, 'serve', '--http=' + address, *app_flags(DATA)]
    origin = env.get('BASE_URL', '').rstrip('/')
    if origin:
        args.append('--origins=' + origin)
    else:
        log('warning: BASE_URL is not set; email links and browser origins are not restricted to the public origin')
    log('starting server on port 80')
    os.execve(SERVER, args, env)


def main(argv=None):
    if os.environ.get('VAULTCONTEXT_DEMO_ONCE') == 'true' and not ONCE_CHILD:
        os.execv('/usr/local/bin/demo-supervisor', ['/usr/local/bin/demo-supervisor'])
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('start', 'init', 'serve', 'verify', 'handoff', 'adopt-handoff'), nargs='?', default='start')
    args = parser.parse_args(argv)
    try:
        demo_fence(args.mode)
        os.chdir(APP)
        if args.mode == 'handoff':
            handoff(DATA)
        elif args.mode == 'adopt-handoff':
            adopt_handoff(DATA)
        elif args.mode == 'serve':
            serve()
        elif args.mode == 'verify':
            validate_config()
            verify(DATA)
            log('database and remote originals verified')
        else:
            prepare(DATA, initialize=args.mode == 'init')
            if args.mode == 'start':
                log('starting Litestream, which starts and supervises the server')
                os.execve(LITESTREAM, [LITESTREAM, 'replicate', '-config', replication_config(),
                                      '-exec', SELF + ' serve'], dict(os.environ))
        return 0
    except StartupError as error:
        print('entrypoint: error: ' + str(error), file=sys.stderr)
    except Exception as error:
        # SDK/subprocess errors may contain credentials, URLs or application data.
        print('entrypoint: startup failed (' + type(error).__name__ + '); operator recovery required', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
