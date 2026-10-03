import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from runner.providers import discover_models, normalize_base, auth_headers


class DiscoveryTests(unittest.TestCase):
    def test_endpoint_normalization(self):
        for url in ('https://gateway.example', 'https://gateway.example/v1/', 'https://gateway.example/v1/messages'):
            self.assertEqual(normalize_base(url, 'anthropic'), 'https://gateway.example/v1')
        self.assertEqual(normalize_base('https://gateway.example/anthropic/v1/models', 'anthropic'), 'https://gateway.example/anthropic/v1')
        self.assertEqual(normalize_base('http://127.0.0.1:11434/api/chat', 'ollama'), 'http://127.0.0.1:11434')

    def test_discovery_http_and_auth(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, dict(self.headers)))
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                if self.path == '/api/tags':
                    data = {'models': [{'name': 'local-coder:latest'}]}
                else:
                    data = {'data': [{'id':'dynamic-model', 'display_name':'Live model'}, {'id':'dynamic-model'}, None]}
                self.wfile.write(json.dumps(data).encode())
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            result = discover_models({'kind':'claude-code','baseUrl':base,'apiKey':'test-key','authMode':'bearer'})
            self.assertEqual(result['models'], [{'id':'dynamic-model','name':'Live model'}])
            self.assertEqual(received[0][0], '/v1/models')
            self.assertEqual(received[0][1]['Authorization'], 'Bearer test-key')
            self.assertEqual(received[0][1]['Anthropic-Version'], '2023-06-01')
            self.assertNotIn('X-Api-Key', received[0][1])
            result = discover_models({'kind':'ollama','baseUrl':base,'authMode':'none'})
            self.assertEqual(result['models'][0]['id'],'local-coder:latest')
            self.assertEqual(received[1][0],'/api/tags')
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_empty_discovery_is_actionable(self):
        with patch('runner.providers.request_json', return_value={'data':[]}):
            with self.assertRaisesRegex(ValueError, 'no models'):
                discover_models({'kind':'custom','baseUrl':'https://example.com'})

    def test_api_key_and_none_modes(self):
        self.assertEqual(auth_headers('key','anthropic')['x-api-key'],'key')
        self.assertNotIn('Authorization',auth_headers('key','responses','none'))
