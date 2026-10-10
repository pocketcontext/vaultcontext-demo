"""The installation contract derives from the packaged public launcher, fail closed."""
import shutil
import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PublicInstallation(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for hook runtime test')
    def test_launcher_pin_contract_and_invalid_sources(self):
        program = r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const launcher=fs.readFileSync('skills/vaultcontext/vaultcontext','utf8');
for(const mode of ['valid','missing','oversized','unpinned','wrong-repository','ambiguous']) {
 const context={module:{exports:{}},toString:value=>String(value),$os:{readFile:path=>{
  assert.equal(path,'skills/vaultcontext/vaultcontext');
  if(mode==='missing')throw Error('missing');
  if(mode==='oversized')return 'x'.repeat(4097);
  if(mode==='unpinned')return launcher.replace(/git@([a-f0-9]{40})/, 'git@main');
  if(mode==='wrong-repository')return launcher.replace('github.com/pocketcontext/vaultcontext-demo.git','github.com/other/client.git');
  if(mode==='ambiguous')return launcher+'\n'+launcher;
  return launcher;
 }}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('pb_hooks/demo.js','utf8'),context);
 const result=context.publicInstallation({});
 assert.equal(!!result,mode==='valid',mode);
 if(result){
  assert.equal(result.method,'skills');assert.equal(result.skill,'vaultcontext');
  assert.equal(result.source,'https://github.com/pocketcontext/vaultcontext-demo/tree/main/skills/vaultcontext');
  assert.equal(result.clientRevision,launcher.match(/git@([a-f0-9]{40})/)[1]);
 }
}
'''
        subprocess.run(['node', '-e', program], cwd=ROOT, check=True)

if __name__ == '__main__': unittest.main()
