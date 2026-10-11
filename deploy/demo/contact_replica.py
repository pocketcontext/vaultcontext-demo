#!/usr/bin/env python3
"""Replicate and restore the isolated durable demo contact database with Litestream."""
import argparse
import ctypes
from datetime import datetime
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time

TABLES = {'contacts','consent_events','suppression','unsubscribe_tokens','security_events','review_events'}


class ReplicaError(RuntimeError):
    pass


def directory_sync(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def atomic(path, value):
    temporary=path.with_name(path.name+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path);directory_sync(path.parent)


def validate_database(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ReplicaError('unsafe contact database')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
            raise ReplicaError('contact database integrity failed')
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        tables-= {'_litestream_seq','_litestream_lock'}
        if tables!=TABLES:
            raise ReplicaError('contact database schema differs')
        # Stable logical digest avoids differences in SQLite/WAL physical layout.
        checksum=hashlib.sha256()
        for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"):
            if row[2] in TABLES:checksum.update(json.dumps(list(row),separators=(',',':')).encode())
        for table in sorted(tables):
            checksum.update(table.encode())
            for row in db.execute('SELECT * FROM "'+table+'" ORDER BY rowid'):
                checksum.update(json.dumps(list(row),separators=(',',':')).encode())
        return checksum.hexdigest()


def child_environment(environ):
    # Litestream receives only contact credentials, never ephemeral replica keys.
    env={key:value for key,value in environ.items()
         if not key.startswith(('VAULTCONTEXT_S3_','LITESTREAM_','AWS_','VAULTCONTEXT_GOOGLE_','VAULTCONTEXT_SUPERUSER_'))}
    for name in ('ACCESS_KEY_ID','SECRET_ACCESS_KEY'):
        if environ.get('CONTACTS_LITESTREAM_'+name):
            env['LITESTREAM_'+name]=environ['CONTACTS_LITESTREAM_'+name]
    return env


def parent_death_guard(parent):
    # Linux runtime: a crashed supervisor must not leave an untracked writer/replicator.
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGTERM,0,0,0)!=0 or os.getppid()!=parent:
        os._exit(1)


class Replica:
    def __init__(self,database,config,litestream,service,port=8781,local_replica=None):
        self.database=Path(database).absolute();self.root=self.database.parent
        for path in (self.database,*self.database.parents):
            if path.is_symlink():raise ReplicaError('symlink contact storage is forbidden')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self.root.stat()
        if info.st_uid!=os.getuid() or info.st_mode&0o077:raise ReplicaError('private contact directory required')
        self.config=str(config);self.litestream=str(litestream);self.service=str(service);self.port=port
        self.socket=self.root/'litestream.sock';self.fence=self.root/'migration-fenced.json'
        self.manifest=self.root/'contact-handoff.json'
        self.env=child_environment(os.environ)
        hours=self.env.get('CONTACTS_LITESTREAM_RETENTION_HOURS','672')
        if not re.fullmatch(r'[0-9]+',hours) or not 1<=int(hours)<=672:
            raise ReplicaError('contact replica retention must be finite, from 1 to 672 hours')
        self.env['CONTACTS_LITESTREAM_RETENTION_HOURS']=hours
        self.env.setdefault('CONTACTS_LITESTREAM_SYNC_INTERVAL','10s')
        if not re.fullmatch(r'[1-9][0-9]?[sm]',self.env['CONTACTS_LITESTREAM_SYNC_INTERVAL']):
            raise ReplicaError('invalid contact sync interval')
        self.env.update(CONTACTS_DATABASE=str(self.database),CONTACTS_REPLICA_SOCKET=str(self.socket))
        if local_replica is not None:
            replica=Path(local_replica).absolute()
            if replica==self.root or self.root in replica.parents or replica in self.root.parents:
                raise ReplicaError('local replica must be separate from live contact state')
            replica.mkdir(mode=0o700,parents=True,exist_ok=True)
            # Explicit file replica for isolated synthetic integration; no cloud fallback.
            self.config=str(self.root/'local-litestream.yml')
            content=('socket:\n  enabled: true\n  path: '+json.dumps(str(self.socket))+'\n  permissions: 0600\n'
                     'snapshot:\n  interval: 1h\n  retention: '+hours+'h\nretention:\n  enabled: true\n'
                     'dbs:\n  - path: '+json.dumps(str(self.database))+'\n    replica:\n      type: file\n      path: '+json.dumps(str(replica))+'\n      sync-interval: '+json.dumps(self.env['CONTACTS_LITESTREAM_SYNC_INTERVAL'])+'\n')
            fd=os.open(self.config,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(content)
        else:
            required=['BUCKET','ENDPOINT','REGION','PATH','ACCESS_KEY_ID','SECRET_ACCESS_KEY']
            if any(not self.env.get('CONTACTS_LITESTREAM_'+key) for key in required):
                raise ReplicaError('dedicated contact replica configuration required')
            if self.env['CONTACTS_LITESTREAM_BUCKET'] != 'once-v2-vaultcontext-demo-contacts-replica' and not re.fullmatch(r'vaultcontext-demo-contacts-[a-z0-9][a-z0-9-]{1,32}',self.env['CONTACTS_LITESTREAM_BUCKET']):
                raise ReplicaError('dedicated durable contact bucket required')
            if not self.env['CONTACTS_LITESTREAM_ENDPOINT'].startswith('https://'):
                raise ReplicaError('contact replica requires HTTPS')
            prefix=self.env['CONTACTS_LITESTREAM_PATH']
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_/-]{0,199}',prefix) or '..' in prefix:
                raise ReplicaError('invalid contact replica prefix')
        self.shutdown=threading.Event();self.replication=None;self.application=None

    def command(self,arguments,timeout=90):
        try:
            result=subprocess.run([self.litestream,*arguments],env=self.env,stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout)
            if result.returncode:raise ReplicaError('contact replica operation failed')
        except Exception:
            raise ReplicaError('contact replica operation failed') from None

    def restore(self,target,optional=False):
        args=['restore','-config',self.config,'-o',str(target),'-integrity-check','full']
        if optional:args+=['-if-replica-exists']
        self.command(args+[str(self.database)])
        if target.exists():
            target.chmod(0o600)
            return validate_database(target)
        if not optional:raise ReplicaError('contact replica is absent')
        return None

    def store(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('demo_retention',self.service)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.Store(self.database)

    def prepare(self,mode):
        if self.fence.exists():
            raise ReplicaError('source contact service is fenced for host migration')
        if (self.root/'initialization.pending').exists() or (self.root/'recovery.pending').exists():
            raise ReplicaError('contact initialization is incomplete; operator recovery required')
        existing=self.database.exists()
        if mode in ('init','adopt-handoff') and existing:
            raise ReplicaError('contact initialization/adoption requires a fresh local database')
        if existing:
            validate_database(self.database)
            return
        if any((self.root/(self.database.name+suffix)).exists() for suffix in ('-wal','-shm','-journal')):
            raise ReplicaError('partial contact database state requires operator recovery')
        if mode!='init':atomic(self.root/'recovery.pending',{'mode':mode})
        with tempfile.TemporaryDirectory(prefix='.contact-restore-',dir=self.root) as tmp:
            staged=Path(tmp)/'contacts.db'
            digest=self.restore(staged,optional=mode=='init')
            if mode=='init':
                if digest is not None:raise ReplicaError('contact replica already exists; restore instead of initializing')
                atomic(self.root/'initialization.pending',{'pending':True})
                # Import the adjacent source module or installed service script.
                self.store()
                validate_database(self.database)
                (self.root/'initialization.pending').unlink();directory_sync(self.root)
                return
            if mode=='adopt-handoff':
                incoming=self.root/'incoming-handoff.json'
                state=json.loads(incoming.read_text())
                if set(state)!={'version','databaseDigest','completedAt'} or state['version']!=1 or state['databaseDigest']!=digest:
                    raise ReplicaError('restored contact replica differs from verified handoff')
            if mode=='start':
                # An asynchronous crash restore is not authority to resurrect consent.
                # Keep every restored contact inactive until fresh explicit consent.
                with closing(sqlite3.connect(staged)) as db:
                    db.execute('PRAGMA secure_delete=ON')
                    db.execute('BEGIN IMMEDIATE')
                    now=int(time.time())
                    for subject,email,revision,terms in db.execute('SELECT subject,email,revision,terms FROM contacts').fetchall():
                        db.execute('INSERT INTO suppression VALUES (?,1,1,?) ON CONFLICT(email) DO UPDATE SET sales=1,newsletter=1,updated=excluded.updated',(email,now))
                        fresh_revision=(1<<51)+secrets.randbits(50)
                        while fresh_revision==revision:fresh_revision=(1<<51)+secrets.randbits(50)
                        db.execute('UPDATE contacts SET sales=0,newsletter=0,revision=? WHERE subject=?',(fresh_revision,subject))
                        db.execute('INSERT INTO consent_events(subject,at,sales,newsletter,terms,consent,revision) VALUES (?,?,0,0,?,?,?)',(subject,now,terms,'recovery-disabled',fresh_revision))
                    db.commit()
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            with staged.open('rb') as stream:os.fsync(stream.fileno())
            os.replace(staged,self.database);directory_sync(self.root)
        if mode=='adopt-handoff':
            # Never remove an old source fence; adoption is a fresh target only.
            if self.fence.exists():raise ReplicaError('cannot adopt on the fenced source host')
            atomic(self.root/'adopted-handoff.json',state)

    def sync(self,timeout=60):
        self.command(['sync','-wait','-timeout',str(timeout),'-socket',str(self.socket),str(self.database)],timeout=timeout+5)

    @staticmethod
    def stop(process):
        if process is None:return
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=45)
            except subprocess.TimeoutExpired:raise ReplicaError('contact process did not stop gracefully') from None
        if process.returncode not in (0,-signal.SIGTERM):raise ReplicaError('contact process failed')

    def replicate(self):
        if self.socket.exists():
            info=self.socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():
                raise ReplicaError('unsafe contact replica socket')
            with socket.socket(socket.AF_UNIX) as probe:
                try:probe.connect(str(self.socket))
                except ConnectionRefusedError:self.socket.unlink()
                else:raise ReplicaError('another contact replica daemon still owns the socket')
        parent=os.getpid()
        self.replication=subprocess.Popen([self.litestream,'replicate','-config',self.config],env=self.env,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            preexec_fn=lambda:parent_death_guard(parent))
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if self.replication.poll() is not None:
                raise ReplicaError('contact replication did not become ready')
            if self.socket.exists():
                try:self.sync(timeout=5);return
                except ReplicaError:pass
            time.sleep(.1)
        raise ReplicaError('contact initial sync did not become ready')

    def verify_remote(self):
        expected=validate_database(self.database)
        with tempfile.TemporaryDirectory(prefix='.contact-verify-',dir=self.root) as tmp:
            actual=self.restore(Path(tmp)/'contacts.db')
        if actual!=expected:raise ReplicaError('contact final replica does not match local state')
        return expected

    def audit_replica_age(self,now=None):
        # Native LTX metadata only: no object names/credentials enter output or logs.
        # This checks current Litestream files, not provider versions or hidden backups.
        now=time.time() if now is None else now
        try:
            result=subprocess.run([self.litestream,'ltx','-config',self.config,'-level','all','-json',str(self.database)],
                env=self.env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=90)
            if result.returncode or len(result.stdout)>16*1024*1024:raise ValueError()
            rows=json.loads(result.stdout)
            if not isinstance(rows,list) or not rows:raise ValueError()
            ages=[(row['level'],now-datetime.fromisoformat(row['timestamp'].replace('Z','+00:00')).timestamp()) for row in rows]
            if any(age < -300 or age > 29*86400 for _,age in ages):raise ValueError()
            if not any(level==9 and -300<=age<=300 for level,age in ages):raise ValueError()
        except Exception:
            raise ReplicaError('contact replica age or fresh snapshot verification failed') from None
        return {'checkedAt':int(now),'oldestListedAgeSeconds':int(max(age for _,age in ages))}

    def refresh_replica(self):
        # Caller drains HTTP and stops its daemon first. The pinned binary rewrites
        # even an unchanged snapshot; scheduled snapshots alone skip idle databases.
        self.command(['replicate','-config',self.config,'-once','-force-snapshot','-enforce-retention'],timeout=180)
        self.verify_remote()
        report=self.audit_replica_age()
        atomic(self.root/'replica-retention-check.json',report)

    def startup_maintenance(self,mode):
        # The ONCE supervisor owns 00:05 maintenance after baseline validation.
        # Candidate/adoption startup must not expire rows before that comparison.
        if mode=='start' and self.env.get('VAULTCONTEXT_DEMO_ONCE_CHILD')!='contacts':
            self.store().maintenance()

    def run(self,mode):
        fd=os.open(self.root/'replication.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ReplicaError('another contact replica process is active') from None
            self.prepare(mode)
            self.startup_maintenance(mode)
            previous={sig:signal.signal(sig,lambda _s,_f:self.shutdown.set()) for sig in (signal.SIGTERM,signal.SIGINT)}
            manifest_value=None
            try:
                self.refresh_replica()
                self.replicate()
                if mode in ('init','adopt-handoff'):
                    self.verify_remote()
                    (self.root/'recovery.pending').unlink(missing_ok=True);directory_sync(self.root)
                    return
                if (self.root/'recovery.pending').exists():
                    self.verify_remote()
                    (self.root/'recovery.pending').unlink();directory_sync(self.root)
                app_env={key:value for key,value in self.env.items() if not key.startswith(('CONTACTS_LITESTREAM_','LITESTREAM_','AWS_'))}
                parent=os.getpid()
                self.application=subprocess.Popen([sys.executable,self.service,'--database',str(self.database),'--port',str(self.port)],
                    env=app_env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    preexec_fn=lambda:parent_death_guard(parent))
                next_refresh=time.monotonic()+86400
                while not self.shutdown.wait(.1):
                    if self.replication.poll() is not None or self.application.poll() is not None:
                        raise ReplicaError('contact service or replication process exited')
                    if time.monotonic()>=next_refresh:
                        self.stop(self.application);self.application=None
                        self.stop(self.replication);self.replication=None
                        self.refresh_replica()
                        self.replicate()
                        self.application=subprocess.Popen([sys.executable,self.service,'--database',str(self.database),'--port',str(self.port)],
                            env=app_env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                            preexec_fn=lambda:parent_death_guard(parent))
                        next_refresh=time.monotonic()+86400
                self.stop(self.application);self.application=None
                self.sync()
                digest=self.verify_remote()
                if self.fence.exists():
                    manifest_value={'version':1,'databaseDigest':digest,'completedAt':int(time.time())}
            finally:
                # Drain writes before stopping replication, even on failures.
                try:self.stop(self.application)
                finally:
                    try:self.stop(self.replication)
                    finally:
                        for sig,callback in previous.items():signal.signal(sig,callback)
            if manifest_value is not None:atomic(self.manifest,manifest_value)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['start','init','adopt-handoff','fence'],nargs='?',default='start')
    parser.add_argument('--database',default='/demo-persistent/contacts.db')
    parser.add_argument('--config',default='/etc/vaultcontext-demo-contacts-litestream.yml')
    parser.add_argument('--litestream',default='/usr/local/bin/litestream')
    parser.add_argument('--service',default='/usr/local/bin/vaultcontext-demo-retention.py')
    parser.add_argument('--port',type=int,default=8781)
    parser.add_argument('--local-replica',help='explicit isolated file replica for synthetic checks; never a cloud fallback')
    args=parser.parse_args();os.umask(0o077)
    try:
        replica=Replica(args.database,args.config,args.litestream,args.service,args.port,args.local_replica)
        if args.mode=='fence':
            # Drain any already-open CLI/HTTP transaction before the marker.
            # Queued Store writers recheck it after acquiring BEGIN IMMEDIATE.
            with closing(sqlite3.connect(replica.database,timeout=30)) as db:
                db.execute('BEGIN IMMEDIATE')
                atomic(replica.fence,{'fenced':True,'requestedAt':int(time.time())})
                replica.manifest.unlink(missing_ok=True)
                db.commit()
        else:replica.run(args.mode)
    except Exception:
        print('contact replication failed; service remains unavailable; operator recovery required',file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':main()
