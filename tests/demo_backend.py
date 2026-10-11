#!/usr/bin/env python3
"""Public demo boundary checks; local provider, private synthetic retention store."""
import argparse
import contextlib
import concurrent.futures
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import integration
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from integration import server as base_server, ROOT
from oauth_integration import google_fixture, REDIRECT

@contextlib.contextmanager
def server(binary,**options):
    # Token minting exists only in a temporary hook tree, never in shipped hooks.
    with tempfile.TemporaryDirectory(prefix='demo-fixture-') as tmp:
        root=Path(tmp)
        for folder in ('pb_hooks','pb_migrations'):
            shutil.copytree(ROOT/folder,root/folder)
        (root/'pb_hooks/demo_tokens.pb.js').write_text("""
routerUse(new Middleware((e)=>{
 if(e.request.url.path==='/api/demo/status' && e.request.url.query().get('syntheticFailure')==='1')
  throw new ApiError(503,'Synthetic health failure',{});
 return e.next();
},1));
onRecordCreate((e)=>{
 if(e.record.getInt('contact_revision')===1)throw new BadRequestError('Synthetic admission failure');
 e.next();
},'demo_enrollments');
routerAdd('POST','/api/synthetic/demo-tokens',(e)=>{
 if(!e.auth || e.auth.collection().name!=='_superusers')throw new ForbiddenError('Test operator required');
 const user=e.app.findRecordById('users',e.requestInfo().body.account);
 user.setPassword('SyntheticFixturePassword123!');e.app.save(user);
 return e.json(200,{auth:user.newAuthToken(),reset:user.newPasswordResetToken(),email:user.newEmailChangeToken('changed@example.test')});
});
""")
        with patch.object(integration,'ROOT',root),base_server(binary,**options) as request:
            yield request

@contextlib.contextmanager
def contacts():
    spec=importlib.util.spec_from_file_location('demo_retention', ROOT/'deploy/demo/retention.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix='demo-contact-test-') as tmp:
        store=module.Store(Path(tmp)/'contacts.db')
        token=secrets.token_urlsafe(32)
        http=ThreadingHTTPServer(('127.0.0.1',0),module.handler(store,token))
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:yield f'http://127.0.0.1:{http.server_port}',token,store
        finally:http.shutdown();http.server_close();thread.join()

def check_installation_metadata(binary):
    today=datetime.now(timezone.utc).date().isoformat()
    with tempfile.TemporaryDirectory(prefix='demo-installation-') as temporary, contacts() as (contact_url,contact_token,_):
        cwd=Path(temporary);shutil.copyfile(ROOT/'pocketcontext.json',cwd/'pocketcontext.json')
        shutil.copytree(ROOT/'pb_public',cwd/'pb_public')
        launcher=cwd/'skills/vaultcontext/vaultcontext';launcher.parent.mkdir(parents=True)
        launcher.write_bytes((ROOT/'skills/vaultcontext/vaultcontext').read_bytes())
        env={'VAULTCONTEXT_DEMO_MODE':'true','VAULTCONTEXT_DEMO_GENERATION':today,
             'VAULTCONTEXT_DEMO_CONTACT_URL':contact_url,'VAULTCONTEXT_DEMO_CONTACT_TOKEN':contact_token}
        with patch.dict(os.environ,env),server(binary,cwd=cwd) as request:
            result=request('GET','/api/demo/status')
            assert result['clientReady'] is True and 'downloads' not in result,result
            assert result['installation']['method']=='skills',result
            assert result['installation']['clientRevision'] in launcher.read_text(),result
            for path in ('/', '/?source=synthetic', '/terms/', '/privacy/'):
                with urllib.request.urlopen(request.base_url+path) as response:
                    assert response.status==200 and response.url==request.base_url+path,response.url
                    assert b'<!doctype html>' in response.read().lower()
            for path in ('/demo','/demo/','/demo/downloads/old.whl','/downloads/manifest.json'):
                try:
                    urllib.request.urlopen(request.base_url+path)
                except urllib.error.HTTPError as error:
                    assert error.code==404,(path,error.code)
                else:
                    raise AssertionError('Removed route remains available: '+path)

        launcher.write_text('invalid unpinned launcher')
        with patch.dict(os.environ,env),server(binary,cwd=cwd) as request:
            result=request('GET','/api/demo/status')
            assert result['clientReady'] is False and result['installation'] is None,result


def check_health_probe_logs(binary):
    today=datetime.now(timezone.utc).date().isoformat()
    probe_token=secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix='demo-probe-logs-') as temporary, contacts() as (contact_url,contact_token,_):
        data_dir=Path(temporary)/'pb_data'
        env={'VAULTCONTEXT_DEMO_MODE':'true','VAULTCONTEXT_DEMO_GENERATION':today,
             'VAULTCONTEXT_DEMO_CONTACT_URL':contact_url,'VAULTCONTEXT_DEMO_CONTACT_TOKEN':contact_token,
             'VAULTCONTEXT_DEMO_HEALTH_TOKEN':probe_token}
        with patch.dict(os.environ,env),server(binary,data_dir=data_dir) as request:
            cases=[('internal',{'X-VaultContext-Health-Token':probe_token},200),
                   ('external',{},200),
                   ('forwarded',{'X-Forwarded-For':'127.0.0.1'},200),
                   ('wrong-token',{'X-VaultContext-Health-Token':'synthetic-wrong-token'},200),
                   ('failure',{'X-VaultContext-Health-Token':probe_token},503)]
            for name,headers,expected in cases:
                path='/api/demo/status?probeCase='+name
                if name=='failure':path+='&syntheticFailure=1'
                req=urllib.request.Request(request.base_url+path,headers=headers)
                try:
                    with urllib.request.urlopen(req) as response:status=response.status
                except urllib.error.HTTPError as error:status=error.code
                assert status==expected,(name,status)
        # Graceful termination flushes the request logger; inspect only synthetic data.
        with contextlib.closing(sqlite3.connect('file:'+str(data_dir/'auxiliary.db')+'?mode=ro',uri=True)) as database:
            logs=[json.loads(row[0]) for row in database.execute('SELECT data FROM _logs')]
        urls=[entry.get('url','') for entry in logs]
        for name,_,_ in cases:
            matching=[url for url in urls if 'probeCase='+name in url]
            assert len(matching)==(0 if name=='internal' else 1),(name,matching,urls)
        assert probe_token not in json.dumps(logs),'Probe token leaked into logs'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True);args=parser.parse_args()
    check_health_probe_logs(args.binary)
    check_installation_metadata(args.binary)
    today=datetime.now(timezone.utc).date().isoformat()
    with contacts() as (contact_url,contact_token,store), google_fixture() as (url,codes), patch.dict(os.environ,{
        'VAULTCONTEXT_DEMO_MODE':'true','VAULTCONTEXT_DEMO_GENERATION':today,
        'VAULTCONTEXT_DEMO_CONTACT_URL':contact_url,'VAULTCONTEXT_DEMO_CONTACT_TOKEN':contact_token,
        'VAULTCONTEXT_GOOGLE_WORKSPACE_DOMAIN':'company.example',
    }), server(args.binary) as request:
        admin=request('POST','/api/collections/_superusers/auth-with-password',{'identity':'admin@example.com','password':'SyntheticAdminPassword123!'})['token']
        provider={'name':'google','clientId':'synthetic-client','clientSecret':'synthetic-secret','authURL':url+'/authorize','tokenURL':url+'/token','userInfoURL':url+'/userinfo'}
        request('PATCH','/api/collections/users',{'oauth2':{'enabled':True,'providers':[provider]}},admin)
        status=request('GET','/api/demo/status');assert status['enabled'] and status['generation']==today and status['clientReady'] is True
        def exchange(email,verified=True,subject=None,expected=200):
            meta=request('GET','/api/collections/users/auth-methods')['oauth2']['providers'][0]
            code=secrets.token_urlsafe(24)
            codes[code]={'challenge':meta['codeChallenge'],'user':{'sub':subject or email,'email':email,'email_verified':verified,'name':'Synthetic Google user'}}
            return request('POST','/api/collections/users/auth-with-oauth2',{'provider':'google','code':code,'redirectURL':REDIRECT,'codeVerifier':meta['codeVerifier'],'createData':{'disabled':True,'demo_google_subject':'forged'}},expected=expected)
        exchange('unverified@gmail.com',verified=False,expected=(400,403))
        alice=exchange('alice@gmail.com');bob=exchange('bob@outside.example')
        assert not alice['record']['disabled'] and 'demo_google_subject' not in alice['record']
        at,bt=alice['token'],bob['token']
        tokens=request('POST','/api/synthetic/demo-tokens',{'account':alice['record']['id']},admin)
        at=tokens['auth']
        # Valid tokens and the correct current password reach the mutation hooks;
        # mere endpoint reachability or malformed-token errors are not evidence.
        request('POST','/api/collections/users/confirm-password-reset',{'token':tokens['reset'],'password':'SyntheticReplacement123!','passwordConfirm':'SyntheticReplacement123!'},expected=403)
        request('POST','/api/collections/users/confirm-email-change',{'token':tokens['email'],'password':'SyntheticFixturePassword123!'},expected=403)
        exchange('alice@gmail.com',subject='another-subject',expected=(400,403))
        request('POST','/api/collections/users/auth-with-password',{'identity':'alice@gmail.com','password':'irrelevant'},expected=(400,403))
        for endpoint in ('request-password-reset','confirm-password-reset','request-email-change','confirm-email-change','auth-with-otp'):
            request('POST','/api/collections/users/'+endpoint,{'email':'alice@gmail.com','token':'invalid','newEmail':'other@example.com','password':'SyntheticPassword123!','passwordConfirm':'SyntheticPassword123!'},at,expected=(400,403))
        def act(token,op,p,expected=200):
            return request('POST','/api/collections/vault_actions/records',{'op':op,'payload':p},token,expected)
        identity={'public_key':'synthetic','signing_key':'synthetic','fingerprint':'synthetic','key_bundle':'synthetic-encrypted'}
        act(at,'identity_init',identity,403)
        request('POST','/api/context/query',{'sql':'SELECT * FROM user_directory'},at,403)
        enroll={'generation':today,'termsVersion':status['termsVersion'],'acceptTerms':True,'salesConsent':False,'newsletterConsent':False,'expectedRevision':0}
        request('POST','/api/demo/enroll',{**enroll,'acceptTerms':False},at,400)
        request('POST','/api/demo/enroll',{**enroll,'generation':'2000-01-01'},at,400)
        for token in (at,bt):
            prefs=request('GET','/api/demo/preferences',token=token);assert prefs['revision']==0
            # Contact consent commits independently; failed vault admission stays closed.
            request('POST','/api/demo/enroll',enroll,token,400)
            act(token,'identity_init',identity,403)
            prefs=request('GET','/api/demo/preferences',token=token);assert prefs['revision']==1
            request('POST','/api/demo/enroll',enroll,token,409)
            result=request('POST','/api/demo/enroll',{**enroll,'expectedRevision':prefs['revision']},token);assert result['enrolled']
            act(token,'identity_init',identity)
            # A repeated init is an identity conflict, not missing enrollment.
            act(token,'identity_init',identity,409)
        request('POST','/api/demo/enroll',enroll,at,409)
        with store.connect() as db:
            events=db.execute('SELECT event FROM security_events').fetchall()
            assert events and all(row['event']=='enrollment_request' for row in events),events
        for token in [*('x'*length for length in (20,31,129,256)), '\U0001f600'*16, '\u00e9'*32, 'x'*32+'\n', ' '*32, '/'*32]:
            request('POST','/api/demo/unsubscribe',{'token':token},expected=400)
        for length in (32,128):
            request('POST','/api/demo/unsubscribe',{'token':'x'*length})
        def query(token,sql):return request('POST','/api/context/query',{'sql':sql},token)
        for token,user in ((at,alice),(bt,bob)):
            for table in ('user_directory','identities'):
                data=query(token,'SELECT id FROM '+table);assert data['rows']==[[user['record']['id']]],data
            for table in ('demo_policy','demo_enrollments'):
                request('POST','/api/context/query',{'sql':'SELECT * FROM '+table},token,expected=(400,403))
        for op in ('identity_rewrap','share','accept','revoke','rotate'):act(at,op,{},403)
        for n in range(3):act(at,'vault_create',{'id':str(n)*15,'metadata':'encrypted','envelope':'encrypted'})
        act(at,'vault_create',{'id':'4'*15,'metadata':'encrypted','envelope':'encrypted'},429)
        # Concurrent create cannot exceed the transactional account quota.
        def create_bob(n):
            try:
                act(bt,'vault_create',{'id':f'b{n:014}','metadata':'encrypted','envelope':'encrypted'})
                return True
            except AssertionError as exc:
                assert ', 429,' in str(exc),exc
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            assert sum(pool.map(create_bob,range(5)))==3
        vault='0'*15
        for n in range(20):
            act(at,'save',{'vault':vault,'document':f'd{n:014}','version':f'v{n:014}','expected_revision':0,'epoch':1,'metadata':'encrypted','manifest':'encrypted','signature':'synthetic','chunks':['synthetic-ciphertext']})
        act(at,'save',{'vault':vault,'document':'z'*15,'version':'y'*15,'expected_revision':0,'epoch':1,'metadata':'encrypted','manifest':'encrypted','signature':'synthetic','chunks':['synthetic-ciphertext']},429)
        assert not query(bt,'SELECT * FROM documents')['rows']
        request('GET','/api/collections/documents/records',token=bt,expected=(400,403,404))
        enrollments=request('GET','/api/collections/demo_enrollments/records',token=admin)['items']
        usage=next(row for row in enrollments if row['account']==alice['record']['id'])
        usage_path='/api/collections/demo_enrollments/records/'+usage['id']
        assert usage['encoded_bytes']==20*len('synthetic-ciphertext')
        # Failed saves roll back both usage and writes. A near-limit fixture exercises
        # the actual byte guard without allocating tens of megabytes of test uploads.
        request('PATCH',usage_path,{'encoded_bytes':status['limits']['encodedBytes']-1},admin)
        save={'vault':vault,'document':'d00000000000000','version':'q'*15,'expected_revision':1,'epoch':1,'metadata':'encrypted','manifest':'encrypted','signature':'synthetic','chunks':['xx']}
        act(at,'save',save,429)
        after=request('GET',usage_path,token=admin)
        assert after['encoded_bytes']==status['limits']['encodedBytes']-1 and after['action_count']==usage['action_count']
        # Withdrawal remains possible across the reset fence and rejects stale reconsent.
        prefs=request('GET','/api/demo/preferences',token=at)
        consent=request('POST','/api/demo/enroll',{**enroll,'salesConsent':True,'expectedRevision':prefs['revision']},at)
        withdrawal=consent['contact']['unsubscribeToken']
        # A persisted stale generation fences every API; no test-only clock hook.
        request('PATCH','/api/collections/demo_policy/records/demopolicy00001',{'generation':'2000-01-01'},admin)
        for method,path,body,token in [('GET','/api/demo/status',None,None),('POST','/api/context/query',{'sql':'SELECT 1'},at),('POST','/api/collections/users/auth-refresh',None,at)]:
            request(method,path,body,token,503)
        request('POST','/api/demo/unsubscribe',{'token':withdrawal})
        assert not store.preferences('alice@gmail.com')['salesContact']
    # Reusing yesterday's state or switching the mode cannot bypass the fence.
    with contacts() as (contact_url,contact_token,_), tempfile.TemporaryDirectory(prefix='demo-restart-') as tmp:
        state=Path(tmp)/'pb_data'
        env={'VAULTCONTEXT_DEMO_MODE':'true','VAULTCONTEXT_DEMO_GENERATION':today,
             'VAULTCONTEXT_DEMO_CONTACT_URL':contact_url,'VAULTCONTEXT_DEMO_CONTACT_TOKEN':contact_token}
        with patch.dict(os.environ,env),server(args.binary,data_dir=state):pass
        # Configuration flags cannot substitute an unrestricted public directory.
        import json
        bad=Path(tmp)/'bad';bad.mkdir()
        config=json.loads((ROOT/'pocketcontext.json').read_text())
        config['snapshot']['filters']['user_directory']='1 = 1'
        (bad/'pocketcontext.json').write_text(json.dumps(config))
        try:
            with patch.dict(os.environ,env),server(args.binary,cwd=bad):
                raise RuntimeError('unsafe SQL policy accepted')
        except AssertionError as exc:
            assert 'private directory SQL policy' in str(exc),str(exc)
        # An inherited production replica environment must never be accepted.
        try:
            with patch.dict(os.environ,{**env,'LITESTREAM_BUCKET':'production-backups'}),server(args.binary):
                raise RuntimeError('production storage accepted')
        except AssertionError as exc:
            assert 'dedicated primary and replica storage' in str(exc),str(exc)
        for changes,message in [({'VAULTCONTEXT_DEMO_MODE':'false'},'cannot run in production'),
                                ({'VAULTCONTEXT_DEMO_GENERATION':'2000-01-01'},'demo is resetting')]:
            try:
                with patch.dict(os.environ,{**env,**changes}),server(args.binary,data_dir=state):
                    raise RuntimeError('unsafe restart succeeded')
            except AssertionError as exc:
                assert message in str(exc),str(exc)
    print('PASS: demo Google admission, explicit consent, private directory, quotas and generation fence')
if __name__=='__main__':main()
