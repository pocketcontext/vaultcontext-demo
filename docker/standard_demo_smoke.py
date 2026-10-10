#!/usr/bin/env python3
"""Synthetic Docker stop-first gate for the shipped Go demo supervisor.

Uses a temporary token-minting hook solely to provision synthetic identities; it
is mounted read-only and never included in the release image. No Google/provider
or ONCE control-plane behavior is claimed by this container lifecycle check.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('storage_smoke', ROOT/'docker/smoke.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class Failure(RuntimeError):
    pass


def docker(*args, env=None, body=None, required=True, timeout=180):
    result = subprocess.run(['docker', *args], input=body, env={**os.environ, **(env or {})},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if required and result.returncode:
        raise Failure('isolated Docker operation failed: '+args[0])
    return result


def control(name, action, body=None):
    result = docker('exec', '-i', name, 'python3', '/usr/local/bin/vaultcontext-demo-once.py',
                    'control', action, body=None if body is None else json.dumps(body).encode()+b'\n', timeout=600)
    return json.loads(result.stdout)


def address(name):
    value = docker('port', name, '80/tcp').stdout.decode().strip()
    if not re.fullmatch(r'127\.0\.0\.1:[0-9]+', value):
        raise Failure('unexpected test listener')
    return 'http://'+value


def ready(name, revision, timeout=240):
    base = address(name)
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        status, _, value = s.http('GET', base+'/up')
        if status == 200:
            if value != {'ready': True, 'revision': revision}:
                raise Failure('readiness identity differs')
            return base
        state = docker('inspect', '--format', '{{.State.Running}}', name).stdout.strip()
        if state != b'true':
            raise Failure('test application exited before readiness')
        time.sleep(.25)
    raise Failure('current demo readiness timed out')


def stop(name):
    docker('stop', '--time', '900', name, timeout=920)
    if docker('inspect', '--format', '{{.State.ExitCode}} {{.State.OOMKilled}}', name).stdout.strip()!=b'0 false':
        raise Failure('demo did not drain cleanly')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--minio-image', default=s.MINIO_IMAGE)
    args=parser.parse_args()
    prefix='vaultcontext-standard-'+secrets.token_hex(5)
    network=prefix+'-network';volume=prefix+'-storage';minio=prefix+'-minio'
    candidate_image=prefix+':candidate'
    containers=[]
    revision=docker('run','--rm','--entrypoint','cat',args.image,'/usr/local/share/vaultcontext-revision').stdout.decode().strip()
    if not re.fullmatch('[a-f0-9]{40}',revision):raise Failure('image lacks immutable revision')
    next_revision='a'*40 if revision!='a'*40 else 'b'*40
    try:
        if not os.environ.get('VAULTCONTEXT_TEST_MINIO_IMAGE'):
            docker('build','--file',str(ROOT/'docker/minio.Dockerfile'),'--tag',args.minio_image,str(ROOT/'docker'),timeout=1200)
        docker('network','create',network);docker('volume','create',volume)
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            tmp=Path(directory)
            # Same tested runtime and schema; only the immutable build identity changes.
            dockerfile=tmp/'Dockerfile'
            dockerfile.write_text('ARG BASE\nFROM ${BASE}\nRUN printf "%s\\n" "'+next_revision+'" > /usr/local/share/vaultcontext-revision\nLABEL org.opencontainers.image.revision="'+next_revision+'"\n')
            docker('build','--build-arg','BASE='+args.image,'--tag',candidate_image,str(tmp),timeout=180)
            certs=tmp/'certs';certs.mkdir()
            generated=subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2',
                '-keyout',str(certs/'private.key'),'-out',str(certs/'public.crt'),'-subj','/CN='+minio,
                '-addext','subjectAltName=DNS:'+minio+',DNS:localhost,IP:127.0.0.1'],capture_output=True)
            if generated.returncode:raise Failure('synthetic TLS fixture generation failed')
            trust=tmp/'trusted-ca.pem'
            trust.write_bytes(docker('run','--rm','--entrypoint','cat',args.image,'/etc/ssl/certs/ca-certificates.crt').stdout+(certs/'public.crt').read_bytes())
            trust.chmod(0o644)
            root_user=secrets.token_hex(12);root_password=secrets.token_hex(24)
            mc={'MC_HOST_test':f'https://{root_user}:{root_password}@127.0.0.1:9000'}
            containers.append(minio)
            docker('run','-d','--name',minio,'--network',network,'-e','MINIO_ROOT_USER','-e','MINIO_ROOT_PASSWORD',
                   '--mount','type=bind,source='+str(certs)+',target=/certs,readonly',
                   args.minio_image,'server','--certs-dir','/certs','/data',env={'MINIO_ROOT_USER':root_user,'MINIO_ROOT_PASSWORD':root_password})
            roles={'VAULTCONTEXT_S3_':'vaultcontext-demo-files','LITESTREAM_':'vaultcontext-demo-replica',
                   'CONTACTS_LITESTREAM_':'vaultcontext-demo-contacts-replica'}
            for _ in range(60):
                result=docker('exec','-e','MC_HOST_test',minio,'mc','--insecure','mb','--ignore-existing',
                              *['test/'+bucket for bucket in roles.values()],env=mc,required=False)
                if not result.returncode:break
                time.sleep(.5)
            else:raise Failure('local MinIO did not start')
            bindings={'VAULTCONTEXT_GOOGLE_CLIENT_ID':'synthetic-client','VAULTCONTEXT_GOOGLE_CLIENT_SECRET':'synthetic-secret',
                      'VAULTCONTEXT_DEMO_CONTACT_TOKEN':secrets.token_hex(32),
                      'LITESTREAM_SYNC_INTERVAL':'1s','CONTACTS_LITESTREAM_SYNC_INTERVAL':'1s'}
            print('Local MinIO ready; configuring three isolated storage roles', flush=True)
            for stem,bucket in roles.items():
                key=secrets.token_hex(12);password=secrets.token_hex(24)
                policy={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:*'],
                        'Resource':['arn:aws:s3:::'+bucket,'arn:aws:s3:::'+bucket+'/*']}]}
                path=tmp/(bucket+'.json');path.write_text(json.dumps(policy))
                docker('cp',str(path),minio+':/tmp/'+path.name)
                docker('exec','-e','MC_HOST_test','-e','FIXTURE_USER','-e','FIXTURE_PASSWORD',minio,'sh','-c',
                       'mc --insecure admin user add test "$FIXTURE_USER" "$FIXTURE_PASSWORD"',env={**mc,'FIXTURE_USER':key,'FIXTURE_PASSWORD':password})
                docker('exec','-e','MC_HOST_test',minio,'mc','--insecure','admin','policy','create','test',bucket,'/tmp/'+path.name,env=mc)
                docker('exec','-e','MC_HOST_test',minio,'mc','--insecure','admin','policy','attach','test',bucket,'--user',key,env=mc)
                bindings.update({stem+'BUCKET':bucket,stem+'ENDPOINT':'https://'+minio+':9000',stem+'REGION':'us-east-1',
                                 stem+'ACCESS_KEY_ID':key,stem+'SECRET_ACCESS_KEY':password})
                if stem!='VAULTCONTEXT_S3_':bindings[stem+'PATH']='synthetic/database'
            fixture_key=secrets.token_hex(32)
            hook=tmp/'synthetic.pb.js'
            hook.write_text("""routerAdd('POST','/api/synthetic/identity',(e)=>{
 if(e.request.header.get('X-Synthetic-Key')!==%s)throw new ForbiddenError('Synthetic fixture required');
 let user;try{user=e.app.findAuthRecordByEmail('users','standard@example.test')}catch(_){
 user=new Record(e.app.findCollectionByNameOrId('users'));user.set('email','standard@example.test');
 user.set('name','Synthetic standard demo');user.set('verified',true);user.setPassword('SyntheticFixturePassword123!');e.app.save(user);
 const link=new Record(e.app.findCollectionByNameOrId('_externalAuths'));link.set('collectionRef',user.collection().id);
 link.set('recordRef',user.id);link.set('provider','google');link.set('providerId','standard-demo-synthetic');e.app.save(link);
 }
 return e.json(200,{token:user.newAuthToken(),id:user.id});
});\n""" % json.dumps(fixture_key))
            hook.chmod(0o644)
            env={'VAULTCONTEXT_DEMO_ONCE':'true','BASE_URL':'https://vault-demo.pocketcontext.com'}
            def launch(name,image):
                containers.append(name)
                command=['run','-d','--name',name,'--network',network,'-p','127.0.0.1::80','-v',volume+':/storage',
                         '--mount','type=bind,source='+str(hook)+',target=/app/pb_hooks/synthetic.pb.js,readonly',
                         '--mount','type=bind,source='+str(trust)+',target=/etc/ssl/certs/ca-certificates.crt,readonly']
                for key in env:command.extend(['-e',key])
                docker(*command,image,env=env)
            print('Storage configured; starting fresh demo control',flush=True)
            old=prefix+'-old';launch(old,args.image)
            for _ in range(120):
                try:control(old,'status');break
                except (RuntimeError,ValueError):time.sleep(.25)
            else:raise Failure('private control unavailable')
            if s.http('GET',address(old)+'/up')[0]==200:raise Failure('unconfigured runtime was ready')
            print('Private control ready; configuring and initializing',flush=True)
            control(old,'configure',bindings)
            print('Configuration accepted; verify fresh-init storage guard',flush=True)
            docker('exec','-i','-e','MC_HOST_test',minio,'mc','--insecure','pipe',
                   'test/vaultcontext-demo-files/guard-fixture',env=mc,body=b'Synthetic unused ciphertext')
            try:control(old,'initialize')
            except Failure:pass
            else:raise Failure('fresh initialization accepted populated primary storage')
            if control(old,'status')['databases']!=0:raise Failure('rejected initialization created databases')
            docker('exec','-e','MC_HOST_test',minio,'mc','--insecure','rm',
                   'test/vaultcontext-demo-files/guard-fixture',env=mc)
            print('Storage guard passed; initialize fresh volume',flush=True)
            control(old,'initialize')
            print('Fresh demo initialized; checking public readiness',flush=True)
            base=ready(old,revision)
            status,_,identity=s.http('POST',base+'/api/synthetic/identity',{},headers={'X-Synthetic-Key':fixture_key})
            if status!=200:raise Failure('synthetic identity failed HTTP '+str(status))
            client=object.__new__(s.Client);client.base=base;client.token=identity['token']
            today=datetime.now(timezone.utc).date().isoformat()
            status,_,public_status=s.http('GET',base+'/api/demo/status')
            if status!=200:raise Failure('demo status unavailable')
            body={'generation':today,'termsVersion':public_status['termsVersion'],'acceptTerms':True,'salesConsent':True,'newsletterConsent':True,'expectedRevision':0}
            status,_,enrolled=s.http('POST',base+'/api/demo/enroll',body,token=client.token)
            if status!=200:raise Failure('synthetic enrollment failed')
            status,_,_=s.http('POST',base+'/api/demo/unsubscribe',{'token':enrolled['contact']['unsubscribeToken']})
            if status!=200:raise Failure('synthetic preference withdrawal failed')
            document,metadata=s.write_record(client,identity['id']);s.check_records(client,document,metadata)
            overlap=prefix+'-overlap';launch(overlap,candidate_image)
            result=docker('wait',overlap,timeout=30)
            if result.stdout.strip()==b'0':raise Failure('overlapping writer acquired volume')
            ready(old,revision)
            stop(old)
            for index in range(2):
                current=prefix+'-candidate-'+str(index);launch(current,candidate_image)
                client.base=ready(current,next_revision)
                s.check_records(client,document,metadata)
                status,_,preferences=s.http('GET',client.base+'/api/demo/preferences',token=client.token)
                if status!=200 or preferences['salesContact'] or preferences['newsletter']:
                    raise Failure('withdrawn contact preferences changed')
                state=control(current,'status')
                if state['databases']!=3 or not state['ready']:raise Failure('three-database runtime not ready')
                stop(current)
            print('PASS: populated Go-supervised Docker stop-first replacement, revision readiness, protected ciphertext, retained withdrawn contacts, three databases, same-revision restart and overlap rejection')
    finally:
        for name in reversed(containers):docker('rm','--force',name,required=False)
        docker('volume','rm',volume,required=False);docker('network','rm',network,required=False)
        docker('image','rm',candidate_image,required=False)


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('FAIL: standard demo container lifecycle ('+(str(error) if isinstance(error,Failure) else type(error).__name__)+')')
        raise SystemExit(1)
