"""Real nginx ingress tests with synthetic headers and a loopback echo backend.

Run inside the built image with VAULTCONTEXT_TEST_REQUIRE_NGINX=true so CI
cannot silently skip the security boundary. No live accounts or state are used.
"""
import http.client
import http.server
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get('VAULTCONTEXT_TEST_ONCE_RUNTIME', ROOT / 'deploy/demo/once_runtime.py'))


class Echo(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        response = json.dumps({'xff': self.headers.get('X-Forwarded-For'),
                               'cf': self.headers.get('CF-Connecting-IP'),
                               'real': self.headers.get('X-Real-IP')}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class ProxyHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nginx = shutil.which('nginx') or ('/usr/sbin/nginx' if Path('/usr/sbin/nginx').is_file() else None)
        if not nginx:
            if os.environ.get('VAULTCONTEXT_TEST_REQUIRE_NGINX') == 'true':
                raise RuntimeError('real nginx is required for this test gate')
            raise unittest.SkipTest('nginx unavailable; required in both native container CI gates')
        spec = importlib.util.spec_from_file_location('proxy_header_runtime', RUNTIME)
        cls.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runtime)
        cls.temporary = tempfile.TemporaryDirectory(prefix='vault-proxy-fixture-')
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        (root / 'control').mkdir()
        cls.echo = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Echo)
        cls.addClassCleanup(cls.echo.server_close)
        cls.addClassCleanup(cls.echo.shutdown)
        threading.Thread(target=cls.echo.serve_forever, daemon=True).start()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            cls.port = listener.getsockname()[1]
        backend = cls.runtime.Backend(root, 'https://synthetic.invalid')
        backend.stop = lambda *_: None
        backend.launch = lambda *_: None
        backend.execute = lambda *_: None
        backend.gate(True)
        config = (root / 'control/nginx.conf').read_text()
        config = config.replace('/tmp/vaultcontext-demo-nginx.pid', str(root / 'nginx.pid'))
        config = config.replace('listen 80;', f'listen 127.0.0.1:{cls.port};')
        config = config.replace('127.0.0.1:8081', f'127.0.0.1:{cls.echo.server_port}')
        config = config.replace('/tmp/client', str(root / 'client')).replace('/tmp/proxy', str(root / 'proxy'))
        for kind in ('fastcgi', 'uwsgi', 'scgi'):
            config = config.replace('/tmp/' + kind, str(root / kind))
        path = root / 'nginx.conf'
        path.write_text(config)
        check = subprocess.run([nginx, '-p', str(root), '-c', str(path), '-t'], capture_output=True, text=True)
        if check.returncode:
            raise RuntimeError('synthetic nginx configuration failed: ' + check.stderr)
        cls.nginx = subprocess.Popen([nginx, '-p', str(root), '-c', str(path), '-g', 'daemon off;'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            cls.nginx.terminate()
            cls.nginx.wait(timeout=10)
        cls.addClassCleanup(stop)
        for _ in range(100):
            if cls.nginx.poll() is not None:
                raise RuntimeError('synthetic nginx configuration failed')
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.1):
                    break
            except OSError:
                time.sleep(.02)
        else:
            raise RuntimeError('synthetic nginx did not start')

    def fetch(self, headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            conn.putrequest('GET', '/')
            for key, value in headers:
                conn.putheader(key, value)
            conn.endheaders()
            response = conn.getresponse()
            body = response.read()
            return response.status, json.loads(body) if response.status == 200 else None
        finally:
            conn.close()

    def assert_identity(self, peer, cf, expected):
        headers = [('X-Real-IP', '198.51.100.199')]
        if peer is not None:
            headers.append(('X-Forwarded-For', peer))
        if cf is not None:
            headers.append(('CF-Connecting-IP', cf))
        status, data = self.fetch(headers)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'xff': expected, 'cf': None, 'real': None})

    def test_direct_peer_ignores_forged_cloudflare_and_real_ip(self):
        for cf in ('192.0.2.55', 'invalid', '192.0.2.55, 192.0.2.56', None):
            with self.subTest(cf=cf):
                self.assert_identity('198.51.100.20', cf, '198.51.100.20')
                self.assert_identity('2001:db8::20', cf, '2001:db8::20')

    def test_non_cloudflare_boundary_and_unspecified_addresses_remain_untrusted(self):
        for peer in ('173.245.47.255', '173.245.64.0', '0.0.0.0', '::'):
            with self.subTest(peer=peer):
                self.assert_identity(peer, '192.0.2.55', peer)

    def test_direct_container_requests_without_forwarding_use_socket_peer(self):
        self.assert_identity(None, '192.0.2.55', '127.0.0.1')

    def test_all_reviewed_cloudflare_ranges_restore_ipv4_and_ipv6_clients(self):
        for cidr in self.runtime.CLOUDFLARE_PROXY_CIDRS:
            peer = str(ipaddress.ip_network(cidr).network_address + 1)
            for client in ('192.0.2.55', '2001:db8::55'):
                with self.subTest(cidr=cidr, client=client):
                    self.assert_identity(peer, client, client)

    def test_cloudflare_missing_or_invalid_client_header_is_rejected(self):
        for client in (None, '', 'invalid', '999.1.1.1', '255.255.255.255',
                       '192.0.2.55:80', '192.0.2.55, 192.0.2.56', '[2001:db8::1]',
                       '2001:db8::1%eth0', '2001:db8:::1', '192.0.2.0/24'):
            headers = [('X-Forwarded-For', '173.245.48.1')]
            if client is not None:
                headers.append(('CF-Connecting-IP', client))
            with self.subTest(client=client):
                self.assertEqual(self.fetch(headers)[0], 400)

    def test_malformed_or_multihop_forwarding_is_rejected(self):
        for peer in ('invalid', '999.1.1.1', '255.255.255.255',
                     '198.51.100.1, 173.245.48.1', '173.245.48.1, 198.51.100.1',
                     '173.245.48.1:443', '173.245.48.0/20', '2001:db8:::1'):
            with self.subTest(peer=peer):
                self.assertEqual(self.fetch([('X-Forwarded-For', peer),
                                            ('CF-Connecting-IP', '192.0.2.55')])[0], 400)

    def test_duplicate_client_or_peer_headers_cannot_choose_an_identity(self):
        self.assertEqual(self.fetch([('X-Forwarded-For', '173.245.48.1'),
                                     ('CF-Connecting-IP', '192.0.2.55'),
                                     ('CF-Connecting-IP', '192.0.2.56')])[0], 400)
        self.assertEqual(self.fetch([('X-Forwarded-For', '198.51.100.20'),
                                     ('X-Forwarded-For', '173.245.48.1'),
                                     ('CF-Connecting-IP', '192.0.2.55')])[0], 400)


if __name__ == '__main__':
    unittest.main()
