import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from http.client import HTTPConnection
from http.server import HTTPServer
import threading

spec=importlib.util.spec_from_file_location('export_relay',Path(__file__).resolve().parents[1]/'deploy/demo/export_relay.py')
relay=importlib.util.module_from_spec(spec);spec.loader.exec_module(relay)

class RelayTests(unittest.TestCase):
    def test_container_resolution(self):
        with patch.object(relay,'run',return_value=b'abc123abc123 once-app-example-abcdef\n'):
            self.assertEqual(relay.container_id('example'),'abc123abc123')
        with patch.object(relay,'run',return_value=b'abc123abc123 once-app-vaultcontext-demo.d32426-896b42\n'):
            self.assertEqual(relay.container_id('vaultcontext-demo.d32426'),'abc123abc123')
        for output in (b'',b'abc123abc123 once-app-example-abcdef\ndef123def123 once-app-example-123abc\n'):
            with patch.object(relay,'run',return_value=output),self.assertRaises(RuntimeError):
                relay.container_id('example')

    def test_audit_permissions_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audit'
            relay.audit(str(path),{'outcome':200})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            path.chmod(0o644)
            with self.assertRaises(RuntimeError): relay.audit(str(path),{})
            link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(OSError): relay.audit(str(link),{})

    def test_http_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            config=SimpleNamespace(source='vaultcontext-demo',uid='0',container_name='example',audit_file=str(Path(directory)/'audit'))
            server=HTTPServer(('127.0.0.1',0),relay.handler(config))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(method,path,headers=None):
                client=HTTPConnection(*server.server_address,timeout=3)
                client.request(method,path,headers=headers or {})
                response=client.getresponse();result=response.status,response.read();client.close();return result
            try:
                with patch.object(relay,'run') as run:
                    self.assertEqual(request('GET','/health')[0],404)
                    self.assertEqual(request('GET','/export/v1?x=y')[0],404)
                    self.assertEqual(request('POST','/export/v1')[0],405)
                    self.assertEqual(request('GET','/export/v1')[0],401)
                    client=HTTPConnection(*server.server_address,timeout=3)
                    client.putrequest('GET','/export/v1')
                    client.putheader('Authorization','Bearer '+'a'*32)
                    client.putheader('Authorization','Bearer '+'b'*32)
                    client.endheaders()
                    response=client.getresponse()
                    self.assertEqual(response.status,401)
                    response.read();client.close()
                    run.assert_not_called()
                metadata={'credentialId':'crm-test','outcome':200,'snapshotId':'a'*32,'counts':{'contacts':0,'consent_events':0,'suppression':0,'review_events':0}}
                token='synthetic_token_'+'x'*32
                with patch.object(relay,'container_id',return_value='abc123abc123'),patch.object(relay,'run',return_value=json.dumps(metadata).encode()+b'\n{}') as run:
                    self.assertEqual(request('GET','/export/v1',{'Authorization':'Bearer '+token}),(200,b'{}'))
                    self.assertNotIn(token,str(run.call_args.args))
                    self.assertEqual(run.call_args.kwargs['input'],('Bearer '+token).encode())
                with patch.object(relay,'container_id',return_value='abc123abc123'),patch.object(relay,'run',return_value=json.dumps(metadata).encode()+b'\nSECRET_BODY'),patch.object(relay,'audit',side_effect=[None,OSError(),None]):
                    status,body=request('GET','/export/v1',{'Authorization':'Bearer '+token})
                    self.assertEqual(status,503)
                    self.assertNotIn(b'SECRET_BODY',body)
                for output in (b'invalid',b'{}\nbody',json.dumps(metadata).encode()+b'\n'+b'x'*(relay.MAX_BYTES+1)):
                    with patch.object(relay,'container_id',return_value='abc123abc123'),patch.object(relay,'run',return_value=output):
                        self.assertEqual(request('GET','/export/v1',{'Authorization':'Bearer '+token})[0],503)
                self.assertNotIn(token,Path(config.audit_file).read_text())
                with patch.object(relay,'audit',side_effect=OSError()),patch.object(relay,'run') as run:
                    self.assertEqual(request('GET','/export/v1',{'Authorization':'Bearer '+token})[0],503)
                    run.assert_not_called()
            finally:
                server.shutdown();server.server_close();thread.join()

if __name__=='__main__': unittest.main()
