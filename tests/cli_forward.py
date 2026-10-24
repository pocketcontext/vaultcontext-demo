#!/usr/bin/env python3
"""Forward exercise of the documented skill through real CLI/terminal prompts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import time
from unittest.mock import patch
from synthetic_auth import cache_test_session
from integration import server
from client_command import client_command


def terminal(argv, env, cwd):
    pid, fd=pty.fork()
    if pid==0:
        os.chdir(cwd)
        os.execve(argv[0],argv,env)
    output=b'';prompts=0;deadline=time.monotonic()+30
    try:
        while time.monotonic()<deadline:
            ready,_,_=select.select([fd],[],[],.1)
            if ready:
                try:part=os.read(fd,65536)
                except OSError:break
                if not part:break
                output+=part
                # Only synthetic passphrases are supplied; no secret appears in argv/env.
                seen=output.lower().count(b'passphrase:')
                while prompts<seen:
                    os.write(fd,b'Synthetic terminal passphrase 123!\n');prompts+=1
            done,status=os.waitpid(pid,os.WNOHANG)
            if done:
                assert os.waitstatus_to_exitcode(status)==0,'terminal command failed'
                pid=0
                break
        else:raise AssertionError('terminal prompt timed out')
        if pid:
            _,status=os.waitpid(pid,0);pid=0
            assert os.waitstatus_to_exitcode(status)==0,'terminal command failed'
        text=output.decode()
        assert 'Synthetic terminal passphrase 123!' not in text,'passphrase echoed'
        # Terminal prompts precede the single JSON response.
        return json.loads(text[text.find('{'):].strip())
    finally:
        os.close(fd)
        if pid:
            os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)


def main():
    p=argparse.ArgumentParser();p.add_argument('--binary',required=True)
    p.add_argument('--client',help='Copy and execute this standalone launcher outside the repository')
    args=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='vault-forward-') as tmp, server(args.binary) as request:
        command=client_command(args.client,tmp)
        admin=request('POST','/api/collections/_superusers/auth-with-password',{'identity':'admin@example.com','password':'SyntheticAdminPassword123!'})['token']
        actors=[]
        for name in ['owner','colleague']:
            user=request('POST','/api/collections/users/records',{'email':name+'@example.com','name':name,'password':'SyntheticUserPassword123!','passwordConfirm':'SyntheticUserPassword123!'},admin)
            env=dict(os.environ,VAULTCONTEXT_URL=request.base_url,VAULTCONTEXT_USER_EMAIL=name+'@example.com',XDG_CACHE_HOME=str(Path(tmp)/'cache'),XDG_STATE_HOME=str(Path(tmp)/'state'))
            env.pop('VAULTCONTEXT_SESSION_DIR',None)
            def cli(*words,env=env):
                result=subprocess.run(command+list(words),env=env,cwd=tmp,capture_output=True,text=True)
                assert result.returncode==0,'CLI failed: '+result.stderr
                return json.loads(result.stdout)
            with patch.dict(os.environ, {'XDG_CACHE_HOME': env['XDG_CACHE_HOME'], 'XDG_STATE_HOME': env['XDG_STATE_HOME']}):
                os.environ.pop('VAULTCONTEXT_SESSION_DIR', None)
                cache_test_session({'url': request.base_url, 'email': name+'@example.com'},
                                   'SyntheticUserPassword123!')
            cli('check')
            initialized=terminal(command+['init'],env,tmp)
            terminal(command+['unlock','--timeout','180'],env,tmp)
            actors.append((user['id'],initialized['fingerprint'],cli,env))
        owner,owner_fp,a,owner_env=actors[0];colleague,colleague_fp,b,colleague_env=actors[1]
        cat_help=subprocess.run(command+['cat','--help'],cwd=tmp,capture_output=True)
        cat_available=cat_help.returncode==0
        assert args.client or cat_available, 'source package must provide cat'
        compare_help=subprocess.run(command+['compare','--help'],cwd=tmp,capture_output=True)
        compare_available=compare_help.returncode==0
        assert args.client or compare_available, 'source package must provide compare'
        try:
            vault=a('create','Synthetic isolated shared project')['id']
            source=Path(tmp)/'arbitrary.binary';content=b'\x00\xff\r\n'+os.urandom(8192);source.write_bytes(content)
            saved=a('save',vault,str(source))
            assert a('list',vault)[0]['name']==str(source)
            relative='./arbitrary.binary'
            renamed=a('save',vault,relative,'--document',saved['id'])
            assert a('list',vault)[0]['name']==relative
            a('save',vault,relative,'--document',saved['id'],'--name','Explicit display name')
            assert a('list',vault)[0]['name']=='Explicit display name'
            assert b('vaults')==[]
            a('archive',saved['id'])
            # Fingerprints originate from each user's own init output and are explicitly exchanged.
            invitation=a('share',vault,colleague,'--role','reader','--fingerprint',colleague_fp)['id']
            b('accept',invitation,'--fingerprint',owner_fp)
            assert b('list',vault)==[]
            assert b('list',vault,'--archived')[0]['id']==saved['id']
            a('unarchive',saved['id'])
            listing=b('list',vault);assert listing[0]['id']==saved['id']
            destination=Path(tmp)/'restored.binary'
            b('restore',saved['id'],'--to',str(destination))
            assert destination.read_bytes()==content
            assert destination.stat().st_mode&0o777==0o600
            assert [entry['name'] for entry in b('history',saved['id'])]==[str(source),relative,'Explicit display name']
            if compare_available:
                def compare(*words, environment=colleague_env):
                    return subprocess.run(command+['compare',saved['id'],str(source),*words],
                                          env=environment,cwd=tmp,capture_output=True,timeout=30)
                result=compare()
                assert result.returncode==0 and result.stderr==b''
                comparison=json.loads(result.stdout)
                assert comparison['same'] is True and comparison['method']=='encrypted-sha256'
                assert set(comparison)=={'document','version','same','method'}
                assert content not in result.stdout and hashlib.sha256(content).hexdigest().encode() not in result.stdout
                source.write_bytes(b'X'+content[1:])
                result=compare()
                assert result.returncode==0 and json.loads(result.stdout)['same'] is False
                source.unlink()
                denied=compare()
                assert denied.returncode!=0 and denied.stdout==b'' and b'Traceback' not in denied.stderr
                source.write_bytes(content)
                source.rename(Path(tmp)/'comparison-target')
                source.symlink_to(Path(tmp)/'comparison-target')
                denied=compare()
                assert denied.returncode!=0 and denied.stdout==b''
                source.unlink()
                (Path(tmp)/'comparison-target').rename(source)
            if cat_available:
                def cat(*words):
                    return subprocess.run(command+['cat',saved['id'],*words],env=colleague_env,
                                          cwd=tmp,capture_output=True,timeout=30)
                result=cat()
                assert result.returncode==0 and result.stdout==content and result.stderr==b''
                source.write_bytes(b'synthetic updated bytes without a newline')
                a('save',vault,str(source),'--document',saved['id'])
                assert cat().stdout==source.read_bytes()
                assert cat('--version',saved['version']).stdout==content
                if compare_available:
                    assert json.loads(compare().stdout)['same'] is True
                    assert json.loads(compare('--version',saved['version']).stdout)['same'] is False
                archived=a('archive',saved['id'])
                assert archived['archived'] is True
                if compare_available:
                    assert json.loads(compare().stdout)['same'] is True
                assert a('archive',saved['id'])==archived
                assert a('list',vault)==[] and b('list',vault)==[]
                assert a('search',vault,'arbitrary')==[]
                for flags in (['--archived'],['--all']):
                    listing=b('list',vault,*flags)
                    assert len(listing)==1 and listing[0]['archived'] is True
                    assert b('search',vault,'arbitrary',*flags)[0]['id']==saved['id']
                assert cat().stdout==source.read_bytes()
                assert cat('--version',saved['version']).stdout==content
                b('restore',saved['id'],'--version',saved['version'],'--to',str(Path(tmp)/'archived-restored'))
                assert (Path(tmp)/'archived-restored').read_bytes()==content
                for words,environment in ((['save',vault,str(source),'--document',saved['id']],owner_env),
                                          (['unarchive',saved['id']],colleague_env),
                                          (['list',vault,'--archived','--all'],owner_env)):
                    denied=subprocess.run(command+words,env=environment,cwd=tmp,capture_output=True)
                    assert denied.returncode!=0 and denied.stdout==b''
                assert len(b('history',saved['id']))==4
                assert a('unarchive',saved['id'])['archived'] is False
                assert a('list',vault)[0]['archived'] is False
                a('save',vault,relative,'--document',saved['id'])
                assert a('list',vault)[0]['name']==relative
                print('VaultContext literal path names and archive CLI: PASS')
                a('revoke',vault,colleague)
                denied=cat()
                assert denied.returncode!=0 and denied.stdout==b''
                assert denied.stderr and b'Traceback' not in denied.stderr
                if compare_available:
                    denied=compare()
                    assert denied.returncode!=0 and denied.stdout==b'' and b'Traceback' not in denied.stderr
                    print('VaultContext copied/packaged CLI comparison, privacy and revocation: PASS')
                print('VaultContext copied/packaged CLI cat and revocation: PASS')
        finally:
            a('lock');b('lock')
        # The launcher has exited before subsequent calls: its detached session must
        # survive, then expire at the requested lifetime without an explicit lock.
        locked=subprocess.run(command+['vaults'],env=env,cwd=tmp,capture_output=True,text=True)
        assert locked.returncode!=0, 'lock left an accessible session'
        if compare_available:
            denied=compare(environment=owner_env)
            assert denied.returncode!=0 and denied.stdout==b'' and b'unlock' in denied.stderr.lower()
        terminal(command+['unlock','--timeout','30'],env,tmp)
        b('vaults')
        deadline=time.monotonic()+35
        time.sleep(31)
        while time.monotonic()<deadline:
            result=subprocess.run(command+['vaults'],env=env,cwd=tmp,capture_output=True,text=True)
            if result.returncode!=0 and 'unlock' in result.stderr.lower():
                break
            time.sleep(1)
        else:
            b('lock')
            raise AssertionError('expired session remained accessible')
    print('VaultContext independent CLI forward exercise: PASS')
if __name__=='__main__':main()
