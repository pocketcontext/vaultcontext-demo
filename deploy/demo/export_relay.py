#!/usr/bin/env python3
"""Operator-installed, loopback-only contact export relay; never logs payloads."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import stat
import subprocess

MAX_BYTES = 16 * 1024 * 1024
# Authorization crosses docker stdin, never argv/environment. Raw child errors are discarded.
CHILD = r"""
import importlib.util,json,sys,urllib.request,urllib.error
source=sys.argv[1]
authorization=sys.stdin.buffer.read(1024).decode('ascii')
spec=importlib.util.spec_from_file_location('retention','/usr/local/bin/'+source+'-retention.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
credential=module.export_credential('/storage/persistent/export-credentials.json',authorization)
request=urllib.request.Request('http://127.0.0.1:8781/export/v1',headers={'Authorization':authorization})
try:
    response=urllib.request.urlopen(request,timeout=60)
except urllib.error.HTTPError as error:
    response=error
body=response.read(16*1024*1024+1)
if len(body)>16*1024*1024: raise ValueError('oversize')
status=response.status
metadata={'credentialId':credential,'outcome':status,'snapshotId':None,'counts':None}
if status==200:
    snapshot=json.loads(body)
    if snapshot.get('source')!=source: raise ValueError('source')
    sid=snapshot.get('snapshotId')
    counts=snapshot.get('counts')
    import re
    if not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{32}',sid): raise ValueError('snapshot')
    if not isinstance(counts,dict) or set(counts)!={'contacts','consent_events','suppression','review_events'} or any(type(v)!=int or v<0 or v>100000 for v in counts.values()): raise ValueError('counts')
    metadata.update(snapshotId=sid,counts=counts)
else:
    body=b'{"error":"export unavailable"}'
sys.stdout.buffer.write(json.dumps(metadata).encode()+b'\n'+body)
"""


def run(args, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=75, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError('private export transport unavailable')
    return result.stdout


def container_id(name):
    # Docker emits only allowlisted IDs/names, never labels or environment.
    rows = run(['docker', 'ps', '--filter', 'label=once', '--format', '{{.ID}} {{.Names}}']).decode().splitlines()
    pattern = re.compile(r'once-app-' + re.escape(name) + r'-[0-9a-f]{6}')
    matches = [row.split()[0] for row in rows if len(row.split()) == 2 and pattern.fullmatch(row.split()[1])]
    if len(matches) != 1 or not re.fullmatch('[a-f0-9]{12,64}', matches[0]):
        raise RuntimeError('ambiguous or unavailable demo container')
    return matches[0]


def audit(path, event):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('unsafe audit file')
        data = (json.dumps({'at': datetime.now(timezone.utc).isoformat(), **event}, separators=(',', ':')) + '\n').encode()
        if os.write(fd, data) != len(data):
            raise RuntimeError('short audit write')
        os.fsync(fd)
        directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(fd)


def handler(config):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'PrivateExport'
        def log_message(self, *args):
            pass
        def reply(self, status, body):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            event = {'event': 'contact_export_relay', 'source': config.source, 'credentialId': None,
                     'snapshotId': None, 'counts': None, 'outcome': 503}
            try:
                if self.path != '/export/v1' or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
                    event['outcome'] = 404
                    audit(config.audit_file, event)
                    return self.reply(404, b'{"error":"not found"}')
                headers = self.headers.get_all('Authorization', [])
                if len(headers) != 1 or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{32,256}', headers[0]):
                    event['outcome'] = 401
                    audit(config.audit_file, event)
                    return self.reply(401, b'{"error":"unauthorized"}')
                # Durable intent precedes any source access. Completion precedes response release.
                audit(config.audit_file, {**event, 'event': 'contact_export_attempt'})
                result = run(['docker', 'exec', '-i', '--user', config.uid, container_id(config.container_name),
                              'python3', '-c', CHILD, config.source], input=headers[0].encode('ascii'))
                meta, body = result.split(b'\n', 1)
                if len(meta) > 4096 or len(body) > MAX_BYTES:
                    raise RuntimeError('oversize response')
                metadata = json.loads(meta)
                if set(metadata) != {'credentialId', 'outcome', 'snapshotId', 'counts'} or type(metadata['outcome']) is not int or not 200 <= metadata['outcome'] <= 599:
                    raise RuntimeError('invalid metadata')
                credential = metadata['credentialId']
                sid = metadata['snapshotId']
                counts = metadata['counts']
                if credential is not None and (not isinstance(credential, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', credential)):
                    raise RuntimeError('invalid credential metadata')
                if sid is not None and (not isinstance(sid, str) or not re.fullmatch('[a-f0-9]{32}', sid)):
                    raise RuntimeError('invalid snapshot metadata')
                if counts is not None and (not isinstance(counts, dict) or set(counts) != {'contacts', 'consent_events', 'suppression', 'review_events'} or any(type(v) is not int or not 0 <= v <= 100000 for v in counts.values())):
                    raise RuntimeError('invalid count metadata')
                event.update(metadata)
                audit(config.audit_file, event)
                return self.reply(event['outcome'], body)
            except Exception:
                try:
                    audit(config.audit_file, {**event, 'outcome': 503})
                except Exception:
                    pass
                return self.reply(503, b'{"error":"private export unavailable"}')
        def reject(self):
            try:
                audit(config.audit_file, {'event': 'contact_export_rejected', 'source': config.source, 'outcome': 405})
                self.reply(405, b'{"error":"method not allowed"}')
            except Exception:
                self.reply(503, b'{"error":"private export unavailable"}')
        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_CONNECT = do_TRACE = reject
    return Handler


class Server(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, choices=['vaultcontext-demo', 'wikicontext-demo'])
    parser.add_argument('--container-name', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--audit-file', required=True)
    args = parser.parse_args()
    args.uid = '10002' if args.source == 'wikicontext-demo' else '0'
    if not re.fullmatch('[a-zA-Z0-9_.-]+', args.container_name) or not os.path.isabs(args.audit_file):
        parser.error('invalid operator configuration')
    audit(args.audit_file, {'event': 'contact_export_relay_started', 'source': args.source})
    Server(('127.0.0.1', args.port), handler(args)).serve_forever()


if __name__ == '__main__':
    main()
