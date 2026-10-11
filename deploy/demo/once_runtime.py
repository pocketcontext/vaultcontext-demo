#!/usr/bin/env python3
"""Single ONCE application: private control socket, one writer, three replicas."""
import argparse
import base64
from collections import Counter
from contextlib import closing
from datetime import datetime,timezone
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from urllib.request import Request,urlopen

IMAGE=re.compile(r'ghcr\.io/pocketcontext/vaultcontext-demo@sha256:[a-f0-9]{64}')
REVISION=re.compile('[a-f0-9]{40}')
PRIMARY={'VAULTCONTEXT_S3_'+key for key in ('BUCKET','ENDPOINT','REGION','ACCESS_KEY_ID','SECRET_ACCESS_KEY')}
REPLICA={'LITESTREAM_'+key for key in ('BUCKET','ENDPOINT','REGION','PATH','ACCESS_KEY_ID','SECRET_ACCESS_KEY')}
CONTACTS={'CONTACTS_LITESTREAM_'+key for key in ('BUCKET','ENDPOINT','REGION','PATH','ACCESS_KEY_ID','SECRET_ACCESS_KEY')}
REQUIRED=PRIMARY|REPLICA|CONTACTS|{'VAULTCONTEXT_GOOGLE_CLIENT_ID','VAULTCONTEXT_GOOGLE_CLIENT_SECRET','VAULTCONTEXT_DEMO_CONTACT_TOKEN'}
OPTIONAL={'VAULTCONTEXT_S3_FORCE_PATH_STYLE','LITESTREAM_SYNC_INTERVAL','CONTACTS_LITESTREAM_RETENTION_HOURS','CONTACTS_LITESTREAM_SYNC_INTERVAL'}
DATABASES=('data.db','auxiliary.db','contacts.db')
MAX_DATABASE=512*1024*1024

# Official https://www.cloudflare.com/ips-v4/ and /ips-v6/, checked 2026-10-10.
# Review updates with real-nginx spoof-resistance tests; never fetch at startup.
CLOUDFLARE_PROXY_CIDRS=(
    '173.245.48.0/20','103.21.244.0/22','103.22.200.0/22','103.31.4.0/22',
    '141.101.64.0/18','108.162.192.0/18','190.93.240.0/20','188.114.96.0/20',
    '197.234.240.0/22','198.41.128.0/17','162.158.0.0/15','104.16.0.0/13',
    '104.24.0.0/14','172.64.0.0/13','131.0.72.0/22',
    '2400:cb00::/32','2606:4700::/32','2803:f800::/32','2405:b500::/32',
    '2405:8100::/32','2a06:98c0::/29','2c0f:f248::/32',
)

def proxy_header_config():
    """Normalize the known ONCE TLS hop, never an arbitrary forwarded chain.

    ONCE's TLS proxy overwrites XFF with its socket peer. Container ingress is
    private to the trusted host. Cloudflare's own client header is authoritative
    only when that peer belongs to a reviewed Cloudflare proxy range.
    Nginx geo treats malformed/empty IP strings as 255.255.255.255, rejected below.
    """
    ranges='\n'.join(cidr+' 1;' for cidr in CLOUDFLARE_PROXY_CIDRS)
    return '''
map $http_x_forwarded_for $demo_peer_ip { "" $remote_addr; default $http_x_forwarded_for; }
geo $demo_peer_ip $demo_peer_valid { default 1; 255.255.255.255 0; }
geo $http_cf_connecting_ip $demo_cf_ip_valid { default 1; 255.255.255.255 0; }
geo $demo_peer_ip $demo_cloudflare_peer { default 0;
'''+ranges+'''
}
map "$demo_peer_valid:$demo_cloudflare_peer:$demo_cf_ip_valid" $demo_reject_ip {
    default 0;
    "0:0:0" 1;
    "0:0:1" 1;
    "1:1:0" 1;
}
map "$demo_cloudflare_peer:$demo_cf_ip_valid" $demo_client_ip {
    "1:1" $http_cf_connecting_ip;
    default $demo_peer_ip;
}
'''

class RuntimeErrorSafe(RuntimeError):pass

def utc_day():return datetime.now(timezone.utc).date().isoformat()

def atomic(path,value):
    temporary=path.with_name('.'+path.name+'.new')
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(descriptor,'w') as stream:
            json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:temporary.unlink(missing_ok=True)

def private_json(path):
    for item in (path,*path.parents):
        if item.is_symlink():raise RuntimeErrorSafe('symlink state is forbidden')
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
        raise RuntimeErrorSafe('private owned state required')
    return json.loads(path.read_text())

def remove(path):
    path.unlink(missing_ok=True)
    descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)

def validate_bindings(values):
    if not isinstance(values,dict) or not REQUIRED<=set(values) or not set(values)<=REQUIRED|OPTIONAL:
        raise RuntimeErrorSafe('complete dedicated runtime bindings required')
    if any(not isinstance(v,str) or not v or any(c in v for c in '\r\n\x00') for v in values.values()):
        raise RuntimeErrorSafe('invalid private binding')
    roles=(('VAULTCONTEXT_S3_BUCKET','once-v2-vaultcontext-demo-files'),('LITESTREAM_BUCKET','once-v2-vaultcontext-demo-replica'),
           ('CONTACTS_LITESTREAM_BUCKET','once-v2-vaultcontext-demo-contacts-replica'))
    for key,expected in roles:
        pattern=r'vaultcontext-demo-contacts-[a-z0-9-]+' if key.startswith('CONTACTS_') else r'vaultcontext-demo-[a-z0-9-]+'
        if values[key]!=expected and not re.fullmatch(pattern,values[key]):raise RuntimeErrorSafe('dedicated bucket required')
    if len({values[k] for k,_ in roles})!=3 or len({values[p+'ACCESS_KEY_ID'] for p in ('VAULTCONTEXT_S3_','LITESTREAM_','CONTACTS_LITESTREAM_')})!=3:
        raise RuntimeErrorSafe('three independent storage identities required')
    if len(values['VAULTCONTEXT_DEMO_CONTACT_TOKEN'])<32:raise RuntimeErrorSafe('strong contact token required')
    hours=values.get('CONTACTS_LITESTREAM_RETENTION_HOURS','672')
    if not hours.isdigit() or not 1<=int(hours)<=672:raise RuntimeErrorSafe('invalid bounded contact retention')
    return values

def spec(value):
    if not isinstance(value,dict) or set(value)!={'image','revision'} or not isinstance(value['image'],str) or not IMAGE.fullmatch(value['image']) or not isinstance(value['revision'],str) or not REVISION.fullmatch(value['revision']):
        raise RuntimeErrorSafe('exact image and revision required')
    return value

def database_snapshot(path,columns=None,immutable=False):
    if path.is_symlink() or not path.is_file():raise RuntimeErrorSafe('all existing databases are required')
    quote=lambda s:'"'+s.replace('"','""')+'"'
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro'+('&immutable=1' if immutable else ''),uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA quick_check').fetchone()!=('ok',):raise RuntimeErrorSafe('database integrity failed')
        if columns is None:
            names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith('_') or r[0] in ('_logs','_externalAuths')]
            columns={name:[r[1] for r in db.execute('PRAGMA table_info('+quote(name)+')')] for name in names}
        hashes={}
        for name,fields in columns.items():
            rows=db.execute('SELECT '+','.join(map(quote,fields))+' FROM '+quote(name))
            values=Counter()
            for row in rows:
                raw=json.dumps([{'bytes':v.hex()} if isinstance(v,bytes) else v for v in row],separators=(',',':'),ensure_ascii=True).encode()
                values[hashlib.sha256(raw).hexdigest()]+=1
            hashes[name]=dict(values)
        return {'columns':columns,'hashes':hashes}

def load_module(name,path):
    loaded=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(loaded);loaded.loader.exec_module(module);return module

class Backend:
    """Local child processes only. No container API, Docker CLI, or ONCE recursion."""
    def __init__(self,root,origin):
        self.root=Path(root);self.origin=origin;self.bindings={};self.processes={};self.lease=None
        self.entrypoint='/usr/local/bin/vaultcontext-entrypoint.py'
        self.contacts='/usr/local/bin/vaultcontext-demo-contact-replica.py'
        self.nginx='/usr/sbin/nginx'
    def configure(self,bindings):self.bindings=dict(bindings)
    def execute(self,args,env=None,timeout=600):
        result=subprocess.run(args,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout,pass_fds=(() if self.lease is None else (self.lease,)))
        if result.returncode:raise RuntimeErrorSafe('child operation failed')
    def launch(self,name,args,env):
        if name in self.processes and self.processes[name].poll() is None:raise RuntimeErrorSafe('child already running')
        self.processes[name]=subprocess.Popen(args,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(() if self.lease is None else (self.lease,)))
    def stop(self,name):
        process=self.processes.get(name)
        if process is None:return
        if process.poll() is None:process.terminate()
        try:code=process.wait(timeout=30 if name=='gate' else 360)
        except subprocess.TimeoutExpired:raise RuntimeErrorSafe('child drain timed out') from None
        self.processes.pop(name,None)
        if code not in (0,-signal.SIGTERM):raise RuntimeErrorSafe('child did not exit cleanly')
    def app_env(self,day):
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':'/root',**{k:v for k,v in self.bindings.items() if not k.startswith('CONTACTS_LITESTREAM_')}}
        env.update(VAULTCONTEXT_DEMO_MODE='true',VAULTCONTEXT_DEMO_ONCE='true',VAULTCONTEXT_DEMO_ONCE_CHILD='app',
                   VAULTCONTEXT_DEMO_ONCE_ROOT=str(self.root),VAULTCONTEXT_DEMO_GENERATION=day,
                   VAULTCONTEXT_DEMO_RESET_FENCE=str(self.root/'control/reset-pending.json'),BASE_URL=self.origin,
                   VAULTCONTEXT_DEMO_CONTACT_URL='http://127.0.0.1:8781',VAULTCONTEXT_TRUSTED_PROXY_HEADER='X-Forwarded-For')
        return env
    def start_app(self,day,initialize=False):
        env=self.app_env(day)
        if initialize:
            atomic(self.root/'control/reset-pending.json',{'deployment':'vaultcontext-demo','generation':day,'phase':'purged'})
            env['VAULTCONTEXT_DEMO_RESET_PHASE']='initialize'
            self.execute([sys.executable,self.entrypoint,'init'],env)
            atomic(self.root/'control/reset-pending.json',{'deployment':'vaultcontext-demo','generation':day,'phase':'initialized'})
            env['VAULTCONTEXT_DEMO_RESET_PHASE']='start'
        self.launch('app',[sys.executable,self.entrypoint],env)
    def stop_app(self):self.stop('app')
    def start_contacts(self,initialize=False):
        if 'contacts' in self.processes and self.processes['contacts'].poll() is None:return
        env={'VAULTCONTEXT_DEMO_ONCE_CHILD':'contacts','PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':'/root',**{k:v for k,v in self.bindings.items() if k.startswith('CONTACTS_LITESTREAM_') or k=='VAULTCONTEXT_DEMO_CONTACT_TOKEN'}}
        args=[sys.executable,self.contacts,'--database',str(self.root/'persistent/contacts.db')]
        if initialize:self.execute(args+['init'],env)
        self.launch('contacts',args+['start'],env)
    def stop_contacts(self):self.stop('contacts')
    def healthy(self):
        for name in ('app','contacts'):
            if name not in self.processes or self.processes[name].poll() is not None:return False
        try:
            with urlopen('http://127.0.0.1:8081/api/demo/status',timeout=2) as response:value=json.load(response)
            request=Request('http://127.0.0.1:8781/health',headers={'Authorization':'Bearer '+self.bindings['VAULTCONTEXT_DEMO_CONTACT_TOKEN']})
            with urlopen(request,timeout=2) as response:contact=json.load(response)
            return value.get('enabled') is True and value.get('generation')==utc_day() and contact.get('ready') is True
        except Exception:return False
    def gate(self,opened):
        self.stop('gate')
        route='''location / { if ($demo_reject_ip) { return 400; }
proxy_pass http://127.0.0.1:8081; proxy_http_version 1.1;
proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto https;
proxy_set_header X-Forwarded-For $demo_client_ip;
proxy_set_header CF-Connecting-IP ""; proxy_set_header X-Real-IP ""; proxy_set_header Connection "";
proxy_buffering off; proxy_request_buffering off; proxy_read_timeout 300s; }''' if opened else 'location / { return 503; }'
        config='''pid /tmp/vaultcontext-demo-nginx.pid;
error_log /dev/null; events { worker_connections 1024; }
http { access_log off; client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
'''+proxy_header_config()+'''
server { listen 80; absolute_redirect off; client_max_body_size 14m;
location = /up { add_header Cache-Control "no-store" always; proxy_pass http://127.0.0.1:8782/ready; }
'''+route+'\n} }\n'
        path=self.root/'control/nginx.conf';path.write_text(config);path.chmod(0o600)
        env={'PATH':'/usr/sbin:/usr/bin:/bin'}
        self.execute([self.nginx,'-t','-c',str(path)],env,30)
        self.launch('gate',[self.nginx,'-c',str(path),'-g','daemon off;'],env)
    def require_empty_primary(self):
        # Initialization is not disaster recovery. Never erase retained ciphertext.
        import boto3
        from botocore.config import Config
        settings={name:self.bindings['VAULTCONTEXT_S3_'+name] for name in ('BUCKET','ENDPOINT','REGION','ACCESS_KEY_ID','SECRET_ACCESS_KEY')}
        if not settings['ENDPOINT'].startswith('https://'):raise RuntimeErrorSafe('primary storage requires HTTPS')
        client=boto3.client('s3',endpoint_url=settings['ENDPOINT'],region_name=settings['REGION'],
            aws_access_key_id=settings['ACCESS_KEY_ID'],aws_secret_access_key=settings['SECRET_ACCESS_KEY'],
            config=Config(connect_timeout=10,read_timeout=30,retries={'max_attempts':3},s3={'addressing_style':'path'}))
        try:
            if client.list_objects_v2(Bucket=settings['BUCKET'],MaxKeys=1).get('Contents') or client.list_multipart_uploads(Bucket=settings['BUCKET'],MaxUploads=1).get('Uploads'):
                raise RuntimeErrorSafe('fresh initialization requires empty primary storage')
        finally:client.close()
    def purge_ephemeral(self):
        reset=load_module('once_reset','/usr/local/lib/vaultcontext-demo/reset.py')
        old={key:os.environ.get(key) for key in PRIMARY|REPLICA}
        try:
            os.environ.update({k:v for k,v in self.bindings.items() if k in PRIMARY|REPLICA})
            for bucket,prefix in ((self.bindings['VAULTCONTEXT_S3_BUCKET'],'VAULTCONTEXT_S3'),(self.bindings['LITESTREAM_BUCKET'],'LITESTREAM')):
                endpoint = self.bindings[prefix+'_ENDPOINT']
                kind = 'r2' if re.fullmatch(r'https://[a-f0-9]{32}(\.eu|\.fedramp)?\.r2\.cloudflarestorage\.com', endpoint) else 's3'
                reset.S3Bucket(bucket,prefix+'_',kind=kind).erase()
        finally:
            for key,value in old.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value
    def maintain_contacts(self):
        self.execute([sys.executable,'/usr/local/bin/vaultcontext-demo-retention.py','--database',str(self.root/'persistent/contacts.db'),'--maintenance'])
    def close(self):
        errors=[]
        for name in ('gate','app','contacts'):
            try:self.stop(name)
            except Exception:errors.append(name)
        if errors:raise RuntimeErrorSafe('children failed to drain')

class Runtime:
    def __init__(self,root,revision,backend,clock=utc_day,compatibility=None):
        if not REVISION.fullmatch(revision):raise RuntimeErrorSafe('immutable build revision required')
        self.root=Path(root).absolute();self.revision=revision;self.backend=backend;self.clock=clock
        self.compatibility=compatibility
        self.control=self.root/'control';self.pending=self.control/'release-pending.json';self.state_file=self.control/'state.json'
        self.ready=False;self.phase='unconfigured';self.generation=None;self.image=None;self.bindings=None;self.lock=None
        self.mutex=threading.RLock();self.stop_event=threading.Event();self.on_locked=None;self.minute=lambda:datetime.now(timezone.utc).hour*60+datetime.now(timezone.utc).minute
    def paths(self):return [self.root/'runtime/pb_data/data.db',self.root/'runtime/pb_data/auxiliary.db',self.root/'persistent/contacts.db']
    def persist(self):
        state={'generation':self.generation,'image':self.image,'revision':self.revision}
        if self.compatibility:state['schema']=self.compatibility['schema']
        atomic(self.state_file,state)
    def date_valid(self):
        if self.generation!=self.clock():raise RuntimeErrorSafe('generation expired')
    def policy_valid(self):
        self.date_valid()
        with closing(sqlite3.connect(self.paths()[0].as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute("SELECT enabled,generation FROM demo_policy WHERE id='demopolicy00001'").fetchone()!=(1,self.generation):raise RuntimeErrorSafe('generation policy differs')
    def baseline(self):return [database_snapshot(path) for path in self.paths()]
    def verify_baseline(self,baseline):
        if not isinstance(baseline,list) or len(baseline)!=len(self.paths()):raise RuntimeErrorSafe('complete three-database baseline required')
        for before in baseline:
            if not isinstance(before,dict) or set(before)!={'columns','hashes'} or not isinstance(before['columns'],dict) or not isinstance(before['hashes'],dict) or set(before['columns'])!=set(before['hashes']):
                raise RuntimeErrorSafe('invalid preservation baseline')
            if not before['columns']:raise RuntimeErrorSafe('empty preservation baseline')
            for name,fields in before['columns'].items():
                if not isinstance(name,str) or not isinstance(fields,list) or not fields or any(not isinstance(field,str) for field in fields):raise RuntimeErrorSafe('invalid baseline columns')
                counts=before['hashes'][name]
                if not isinstance(counts,dict) or any(not re.fullmatch('[a-f0-9]{64}',digest) or type(count)!=int or count<1 for digest,count in counts.items()):raise RuntimeErrorSafe('invalid baseline records')
        for path,before in zip(self.paths(),baseline):
            after=database_snapshot(path,before['columns'])
            if any(Counter(before['hashes'][table])-Counter(after['hashes'][table]) for table in before['hashes']):raise RuntimeErrorSafe('previous records changed or disappeared')
    def configured(self):
        if self.bindings is None:raise RuntimeErrorSafe('private configuration required')
    def start(self):
        with self.mutex:
            for path in (self.root,*self.root.parents):
                if path.is_symlink():raise RuntimeErrorSafe('unsafe storage root')
            self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
            for name in ('control','runtime','persistent'):(self.root/name).mkdir(mode=0o700,exist_ok=True)
            self.lock=open(self.control/'writer.lock','a+')
            try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:self.lock.close();self.lock=None;raise RuntimeErrorSafe('another supervisor owns the volume') from None
            self.backend.lease=self.lock.fileno()
            self.backend.gate(False)
            if self.on_locked:self.on_locked()
            state=None
            if (self.control/'runtime.json').exists():
                self.bindings=validate_bindings(private_json(self.control/'runtime.json'));self.backend.configure(self.bindings)
            if self.compatibility and self.bindings is None and (self.state_file.exists() or any(path.exists() for path in self.paths()) or any((self.control/name).exists() for name in ('upgrade-pending.json','release-pending.json','reset-pending.json','adoption-pending.json'))):
                raise RuntimeErrorSafe('existing deployment has lost its private configuration')
            supplied={key:os.environ[key] for key in REQUIRED|OPTIONAL if key in os.environ}
            if supplied:
                validate_bindings(supplied)
                if self.bindings is not None and supplied!=self.bindings:
                    raise RuntimeErrorSafe('environment differs from bound volume configuration')
                if self.bindings is None:
                    atomic(self.control/'runtime.json',supplied);self.bindings=supplied;self.backend.configure(supplied)
            if self.state_file.exists():
                state=private_json(self.state_file)
                if not isinstance(state,dict) or set(state) not in ({'generation','image','revision'},{'generation','image','revision','schema'}) or not isinstance(state.get('generation'),str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',state['generation']) or not isinstance(state.get('revision'),str) or not REVISION.fullmatch(state['revision']):raise RuntimeErrorSafe('invalid durable state')
                if state['image'] is not None and (not isinstance(state['image'],str) or not IMAGE.fullmatch(state['image'])):raise RuntimeErrorSafe('invalid durable image')
                if 'schema' in state and (not isinstance(state['schema'],str) or not re.fullmatch('[a-f0-9]{64}',state['schema'])):raise RuntimeErrorSafe('invalid durable schema')
                self.generation=state['generation'];self.image=state['image']
            if self.compatibility and state is None and (any(path.exists() for path in self.paths()) or (self.control/'upgrade-pending.json').exists()):
                self.phase='blocked';return self.status()
            if (self.control/'adoption-pending.json').exists() or (self.control/'reset-pending.json').exists():self.phase='blocked';return self.status()
            if self.pending.exists():
                if self.compatibility:self.phase='blocked';return self.status()
                pending=private_json(self.pending)
                if pending['target']['revision']!=self.revision or pending['phase'] not in ('prepared','validated') or self.generation!=self.clock():self.phase='blocked';return self.status()
                self.image=pending['target']['image'];self.phase='candidate'
                try:self.start_writers();self.verify_baseline(private_json(self.control/'release-baseline.json'))
                except BaseException:self.drain();self.phase='blocked';raise
                pending['phase']='validated';atomic(self.pending,pending);self.phase='validated';return self.status()
            if self.compatibility and state:
                return self.standard_start(state)
            if state and state['revision']!=self.revision:self.phase='blocked';return self.status()
            if self.bindings and all(path.exists() for path in self.paths()):
                if self.generation==self.clock():self.start_writers();self.open_gate()
                else:self.reset_day()
            elif self.bindings:self.phase='awaiting-adoption'
            return self.status()
    def standard_start(self,state):
        """Only declared-compatible revisions can replace the stopped application.

        A journal in 'starting' is deliberately not replayed: candidate migrations
        may have committed. Preserve it for explicit recovery, never roll back.
        """
        journal=self.control/'upgrade-pending.json'
        compatible=(state.get('schema') in self.compatibility['acceptSchemas'] or
                    ('schema' not in state and state['revision'] in self.compatibility['acceptLegacyRevisions']))
        if not compatible or not self.bindings or not all(path.exists() for path in self.paths()):
            self.phase='blocked';return self.status()
        if journal.exists():
            pending=private_json(journal)
            if not isinstance(pending,dict) or set(pending)!={'format','source','target','schema','phase','baseline'} or pending.get('format')!=1 or not isinstance(pending.get('source'),str) or not REVISION.fullmatch(pending['source']):
                raise RuntimeErrorSafe('invalid upgrade journal')
            self.verify_baseline(pending['baseline'])
            if pending.get('target')!=self.revision or pending.get('schema')!=self.compatibility['schema'] or pending.get('phase') not in ('prepared','validated'):
                self.phase='blocked';return self.status()
        else:
            pending={'format':1,'source':state['revision'],'target':self.revision,'schema':self.compatibility['schema'],'phase':'prepared','baseline':self.baseline()}
            atomic(journal,pending)
        self.image=None  # Image selection belongs to the container manager.
        try:
            # An old UTC generation is disposed only after compatibility has passed.
            # Reset has its own durable fence and retains the contacts database.
            if self.generation!=self.clock():
                pending['phase']='starting';atomic(journal,pending)
                self.reset_day()
                # Disposable tables changed deliberately; retained contacts must survive.
                after=self.baseline()
                before=pending['baseline'][2]
                contact=database_snapshot(self.paths()[2],before['columns'])
                if any(Counter(before['hashes'][table])-Counter(contact['hashes'][table]) for table in before['hashes']):raise RuntimeErrorSafe('retained contact records changed during upgrade reset')
                pending['baseline']=after
            else:
                pending['phase']='starting';atomic(journal,pending)
                self.start_writers();self.verify_baseline(pending['baseline'])
            pending['phase']='validated';atomic(journal,pending)
            self.persist();remove(journal);self.open_gate()
            return self.status()
        except BaseException:
            self.drain();self.phase='blocked';raise
    def configure(self,bindings):
        with self.mutex:
            if self.bindings is not None or any(path.exists() for path in self.paths()):raise RuntimeErrorSafe('configuration is already bound')
            values=validate_bindings(bindings);atomic(self.control/'runtime.json',values)
            self.bindings=values;self.backend.configure(values);self.phase='awaiting-adoption';return {'configured':True}
    def wait_health(self):
        deadline=time.monotonic()+360
        while not self.backend.healthy():
            if self.stop_event.is_set():raise RuntimeErrorSafe('termination requested during startup')
            self.date_valid()
            if time.monotonic()>deadline:raise RuntimeErrorSafe('replicated application readiness failed')
            time.sleep(.2)
        self.policy_valid()
    def start_writers(self):
        self.configured();self.policy_valid();self.backend.start_contacts();self.backend.start_app(self.generation);self.wait_health()
    def drain(self):
        self.ready=False
        errors=[]
        for operation in (lambda:self.backend.gate(False),self.backend.stop_app,self.backend.stop_contacts):
            try:operation()
            except Exception:errors.append(True)
        if errors:raise RuntimeErrorSafe('writer drain failed')
    def open_gate(self):
        if self.stop_event.is_set():raise RuntimeErrorSafe('termination requested before readiness')
        self.policy_valid();self.persist()
        if (self.control/'upgrade-pending.json').exists():return
        self.backend.gate(True);self.date_valid();self.ready=True;self.phase='active'
    def initialize(self):
        with self.mutex:
            self.configured()
            if self.state_file.exists() or any(path.exists() for path in self.paths()) or any((self.control/name).exists() for name in ('release-pending.json','upgrade-pending.json','reset-pending.json','adoption-pending.json')):raise RuntimeErrorSafe('initialization requires a genuinely empty installation')
            self.backend.require_empty_primary()
            self.backend.start_contacts(initialize=True)
            self.reset_day(initial=True);return self.status()
    def reset_day(self,initial=False):
        if self.pending.exists() or (self.control/'adoption-pending.json').exists():raise RuntimeErrorSafe('pending operation blocks reset')
        self.ready=False;self.phase='resetting';self.backend.gate(False);self.backend.stop_app()
        day=self.clock();atomic(self.control/'reset-pending.json',{'deployment':'vaultcontext-demo','generation':day,'phase':'pending'})
        try:
            if not initial:self.backend.purge_ephemeral()
            shutil.rmtree(self.root/'runtime');(self.root/'runtime/pb_data').mkdir(mode=0o700,parents=True)
            self.generation=day
            if not initial:
                self.backend.stop_contacts();self.backend.start_contacts()
            self.backend.start_app(day,initialize=True);self.wait_health();self.persist()
            remove(self.control/'reset-pending.json');self.open_gate()
        except BaseException:
            self.ready=False;self.phase='blocked';self.backend.gate(False);self.backend.stop_app();raise
    def prepare_release(self,value):
        with self.mutex:
            target=spec(value);self.date_valid()
            if not self.ready or self.pending.exists():raise RuntimeErrorSafe('active unfenced generation required')
            state={'target':target,'generation':self.generation,'phase':'draining'}
            try:
                atomic(self.pending,state);self.drain();atomic(self.control/'release-baseline.json',self.baseline())
                self.date_valid();state['phase']='prepared';atomic(self.pending,state);self.phase='prepared'
                return {'prepared':True,**target,'generation':self.generation}
            except BaseException:self.drain();self.phase='blocked';raise
    def validate_release(self,value):
        with self.mutex:
            target=spec(value);pending=private_json(self.pending)
            if target!=pending['target'] or target['revision']!=self.revision or self.phase!='validated':raise RuntimeErrorSafe('candidate is not validated')
            self.policy_valid();self.verify_baseline(private_json(self.control/'release-baseline.json'))
            return {'validated':True,**target,'generation':self.generation}
    def commit_release(self,value):
        with self.mutex:
            result=self.validate_release(value)
            try:
                self.open_gate();remove(self.control/'release-baseline.json');remove(self.pending)
            except BaseException:
                self.drain();self.phase='blocked';raise
            return {'ready':True,'revision':result['revision'],'image':result['image'],'generation':self.generation}
    def abort_release(self,value):
        with self.mutex:
            target=spec(value)
            if self.pending.exists():
                if private_json(self.pending)['target']!=target:raise RuntimeErrorSafe('different release is pending')
            elif self.image!=target['image'] or self.revision!=target['revision']:
                raise RuntimeErrorSafe('different release is active')
            try:atomic(self.pending,{'target':target,'generation':self.generation,'phase':'aborted'})
            finally:self.drain();self.phase='blocked'
            return {'aborted':True,**target,'generation':self.generation}
    def adoption_header(self,value):
        if not isinstance(value,dict) or set(value)!={'generation','image','revision','files'} or value['generation']!=self.clock() or value['revision']!=self.revision:
            raise RuntimeErrorSafe('invalid adoption generation or revision')
        spec({'image':value['image'],'revision':value['revision']})
        if set(value['files'])!=set(DATABASES):raise RuntimeErrorSafe('exactly three database files required')
        for info in value['files'].values():
            if set(info)!={'size','sha256'} or type(info['size']) is not int or not 1<=info['size']<=MAX_DATABASE or not re.fullmatch('[a-f0-9]{64}',info['sha256']):raise RuntimeErrorSafe('invalid bounded database description')
        return value
    def adopt_begin(self,value):
        with self.mutex:
            self.configured();header=self.adoption_header(value)
            if any(path.exists() for path in self.paths()) or self.pending.exists() or (self.control/'adoption-pending.json').exists():raise RuntimeErrorSafe('adoption requires an empty fenced installation')
            stage=self.root/'runtime/adoption';stage.mkdir(mode=0o700)
            atomic(self.control/'adoption-pending.json',header);self.phase='adopting';return {'accepted':True}
    def adopt_finish(self):
        with self.mutex:
            header=self.adoption_header(private_json(self.control/'adoption-pending.json'));stage=self.root/'runtime/adoption'
            for name in DATABASES:
                path=stage/name;info=header['files'][name]
                if path.is_symlink() or path.stat().st_size!=info['size']:raise RuntimeErrorSafe('adoption file size differs')
                digest=hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(65536),b''):digest.update(chunk)
                if digest.hexdigest()!=info['sha256']:raise RuntimeErrorSafe('adoption file checksum differs')
                # Imported files are verified self-contained SQLite backups. An
                # immutable reader must not create WAL/SHM beside staging files.
                database_snapshot(path,immutable=True)
            self.generation=header['generation'];self.image=header['image']
            (self.root/'runtime/pb_data').mkdir(mode=0o700,exist_ok=True)
            for name,path in zip(DATABASES,self.paths()):os.replace(stage/name,path);path.chmod(0o600)
            try:
                self.policy_valid();self.persist();baseline=self.baseline();self.start_writers();self.verify_baseline(baseline)
                self.open_gate();stage.rmdir();remove(self.control/'adoption-pending.json');return self.status()
            except BaseException:self.drain();self.phase='blocked';raise
    def status(self):
        return {'ready':self.ready,'phase':self.phase,'generation':self.generation,'revision':self.revision,'image':self.image,
                'configured':self.bindings is not None,'databases':sum(path.is_file() for path in self.paths())}
    def tick(self):
        with self.mutex:
            if self.ready and self.generation!=self.clock():self.reset_day()
            elif self.ready and not self.backend.healthy():self.drain();self.phase='blocked'
            if self.ready and self.minute()>=5 and not self.pending.exists():
                marker=self.control/'maintenance.json'
                if not marker.exists() or private_json(marker).get('generation')!=self.generation:
                    try:
                        self.backend.gate(False);self.ready=False
                        self.backend.maintain_contacts();self.date_valid()
                        atomic(marker,{'generation':self.generation});self.open_gate()
                    except BaseException:self.drain();self.phase='blocked';raise
    def close(self):
        with self.mutex:
            self.ready=False
            self.backend.close()
            if self.lock:self.lock.close();self.lock=None


def request(root,command,value=None):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(660);connection.connect(str(root/'control/control.sock'))
        connection.sendall(json.dumps({'command':command,'value':value}).encode()+b'\n');stream=connection.makefile('rb');raw=stream.readline(65537)
        result=json.loads(raw)
        if not result.get('ok'):raise RuntimeErrorSafe('control operation failed')
        return result['result']

def serve_control(runtime):
    path=runtime.control/'control.sock';path.unlink(missing_ok=True)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(str(path));path.chmod(0o600);listener.listen(8);listener.settimeout(.5)
    methods={'status':lambda value:runtime.status(),'configure':runtime.configure,'initialize':lambda value:runtime.initialize(),
             'prepare-release':runtime.prepare_release,'validate-release':runtime.validate_release,'commit-release':runtime.commit_release,'abort-release':runtime.abort_release,
             'adopt-begin':runtime.adopt_begin,'adopt-finish':lambda value:runtime.adopt_finish()}
    if runtime.compatibility:
        for name in ('prepare-release','validate-release','commit-release','abort-release'):methods.pop(name)
    try:
        while not runtime.stop_event.is_set():
            try:connection,_=listener.accept()
            except socket.timeout:continue
            with connection:
                connection.settimeout(660)
                try:
                    raw=connection.makefile('rb').readline(65537)
                    if len(raw)>65536:raise ValueError()
                    data=json.loads(raw)
                    if set(data)!={'command','value'} or data['command'] not in methods:raise ValueError()
                    with runtime.mutex:result=methods[data['command']](data['value'])
                    answer={'ok':True,'result':result}
                except Exception:answer={'ok':False}
                try:connection.sendall(json.dumps(answer).encode()+b'\n')
                except OSError:pass
    finally:listener.close();path.unlink(missing_ok=True)

def control(root,command):
    if command=='adopt':
        raw=sys.stdin.buffer.readline(65537)
        if len(raw)>65536:raise RuntimeErrorSafe('adoption header too large')
        header=json.loads(raw);request(root,'adopt-begin',header)
        for name in DATABASES:
            size=header['files'][name]['size'];path=root/'runtime/adoption'/name
            descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(descriptor,'wb') as stream:
                while size:
                    chunk=sys.stdin.buffer.read(min(size,65536))
                    if not chunk:raise RuntimeErrorSafe('incomplete adoption stream')
                    stream.write(chunk);size-=len(chunk)
                stream.flush();os.fsync(stream.fileno())
        if sys.stdin.buffer.read(1):raise RuntimeErrorSafe('unexpected trailing adoption bytes')
        return request(root,'adopt-finish')
    value=None
    if command not in ('status','initialize'):
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise RuntimeErrorSafe('control request too large')
        value=json.loads(raw)
    return request(root,command,value)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=('run','control'))
    parser.add_argument('command',nargs='?',choices=('status','configure','initialize','adopt','prepare-release','validate-release','commit-release','abort-release'))
    args=parser.parse_args();os.umask(0o077);root=Path('/storage')
    if args.mode=='control':print(json.dumps(control(root,args.command)));return
    revision=Path('/usr/local/share/vaultcontext-revision').read_text().strip()
    origin=os.environ.get('BASE_URL','')
    if not re.fullmatch(r'https://(vault-demo|demo-vault|vaultcontext-demo)\.[a-z0-9.-]+',origin):raise RuntimeErrorSafe('exact HTTPS demo origin required')
    compatibility=json.loads(Path('/usr/local/share/vaultcontext-compatibility.json').read_text())
    if compatibility.get('format')!=1 or not re.fullmatch('[a-f0-9]{64}',compatibility.get('schema','')):
        raise RuntimeErrorSafe('invalid compatibility policy')
    runtime=Runtime(root,revision,Backend(root,origin),compatibility=compatibility)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda _s,_f:runtime.stop_event.set())
    try:
        runtime.on_locked=lambda:threading.Thread(target=serve_control,args=(runtime,),daemon=True).start()
        runtime.start()
        while not runtime.stop_event.wait(.2):runtime.tick()
    finally:runtime.close()

if __name__=='__main__':
    try:main()
    except Exception:
        print('ONCE demo runtime failed; public access is fenced; private operator review required',file=sys.stderr);sys.exit(1)
