import http.client
import http.server
import importlib.util
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

SAVE = '/api/v1/kernels.KernelsApiService/SaveKernel'


def load_proxy():
    path = Path(__file__).resolve().parents[1] / 'python/native/proxy.py'
    with patch.dict(os.environ, {'ORX_KAGGLE_HOME': 'unused-fixture'}), patch.dict(sys.modules, {
        'account_store': SimpleNamespace(token_files=lambda: []),
        'provider_metrics': SimpleNamespace(record=Mock()),
    }), patch.object(sys, 'argv', ['proxy.py']):
        spec = importlib.util.spec_from_file_location('test_bundled_proxy', path)
        proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy)
    return proxy


def serve(handler):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def closed_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.proxy = load_proxy()
        self.proxy.tokens[:] = [['qa.txt', 'KGAT_QA', 0, 0.0]]
        self.proxy.refresh_tokens = lambda: None
        self.server = serve(self.proxy.H)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def post(self, path=SAVE):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        conn.request('POST', path, body=b'{}', headers={'Authorization': 'Bearer KGAT_QA',
                                                        'Content-Type': 'application/json'})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def test_an_unreachable_kaggle_is_retried_then_reported_as_unsent(self):
        self.proxy.POOL = self.proxy._UpstreamPool('http://127.0.0.1:%d' % closed_port())
        with patch.object(self.proxy, 'CONNECT_ATTEMPTS', 2), patch.object(self.proxy.time, 'sleep'):
            status, headers, body = self.post()
        self.assertEqual(status, 502)
        self.assertEqual(headers.get('X-Orx-Upstream-Unsent'), '1')
        self.assertIn(b'request not sent', body)
        self.assertEqual(self.proxy.POOL._created, 2)

    def test_a_failure_after_connecting_is_not_reported_as_unsent(self):
        class Drop(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.connection.close()  # Kaggle may have received the save.
            def log_message(self, *args):
                pass
        upstream = serve(Drop)
        self.addCleanup(upstream.shutdown)
        self.addCleanup(upstream.server_close)
        self.proxy.POOL = self.proxy._UpstreamPool('http://127.0.0.1:%d' % upstream.server_port)
        status, headers, _ = self.post()
        self.assertEqual(status, 502)
        self.assertNotIn('X-Orx-Upstream-Unsent', headers)

    def test_an_explicit_token_receives_kaggle_rate_limit_instead_of_a_block(self):
        calls = []
        class Limited(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def do_POST(self):
                calls.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
                body = b'{"message":"Too many requests"}'
                self.send_response(429)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        upstream = serve(Limited)
        self.addCleanup(upstream.shutdown)
        self.addCleanup(upstream.server_close)
        self.proxy.POOL = self.proxy._UpstreamPool('http://127.0.0.1:%d' % upstream.server_port)
        for _ in range(2):
            status, _, body = self.post('/api/v1/kernels.KernelsApiService/ListKernels')
            self.assertEqual((status, body), (429, b'{"message":"Too many requests"}'))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.proxy.tokens[0][3], 0.0)


if __name__ == '__main__':
    unittest.main()
