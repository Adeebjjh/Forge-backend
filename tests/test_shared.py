"""Shared backend mode: silent device provisioning, per-device workspaces, key injection."""
import json
import os
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from runner.server import State, handler


class SharedServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict(os.environ, {'FORGE_SHARED': '1', 'FORGE_MAX_DEVICES': '10',
                                           'FORGE_MAX_RUNS': '4',
                                           'FORGE_SKILLS_DIR': str(tempfile.mkdtemp())})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.environ.pop, 'GITHUB_TOKEN', None)
        self.master = 'master-token-long-enough-for-pairing'
        self.state = State(self.temp.name, self.master)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def req(self, path, body=None, token='__master__', raw=False):
        headers = {}
        if token == '__master__':
            token = self.master
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.url + path,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
        with urllib.request.urlopen(request) as r:
            data = r.read()
            return data if raw else json.loads(data)

    def provision(self, device):
        return self.req('/api/provision', {'device': device}, token=None)['token']

    def test_provision_happy_path(self):
        tok = self.provision('device-aaa-1')
        self.assertGreaterEqual(len(tok), 32)
        self.assertTrue((self.state.workspace.root / 'devices' / 'device-aaa-1' / 'token').is_file())
        self.assertEqual(self.state.device_tokens[tok], 'device-aaa-1')

    def test_provision_rejects_bad_device(self):
        for bad in ('', 'ab', 'has space', '../x', 'a' * 65):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.req('/api/provision', {'device': bad}, token=None)
            self.assertEqual(ctx.exception.code, 400)

    def test_provision_duplicate(self):
        self.provision('device-dup-1')
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.req('/api/provision', {'device': 'device-dup-1'}, token=None)
        self.assertEqual(ctx.exception.code, 409)

    def test_provision_device_cap(self):
        with patch.dict(os.environ, {'FORGE_MAX_DEVICES': '1'}):
            self.provision('device-cap-1')
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.req('/api/provision', {'device': 'device-cap-2'}, token=None)
            self.assertEqual(ctx.exception.code, 429)

    def test_device_workspace_isolation(self):
        ta = self.provision('device-iso-a')
        tb = self.provision('device-iso-b')
        self.req('/api/file', {'path': 'note.txt', 'content': 'hello-a'}, token=ta)
        files_a = self.req('/api/files', token=ta)['files']
        files_b = self.req('/api/files', token=tb)['files']
        self.assertIn('note.txt', files_a)
        self.assertNotIn('note.txt', files_b)

    def test_bad_token_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.req('/api/files', token='nope-not-a-token')
        self.assertEqual(ctx.exception.code, 401)

    def test_master_token_sees_root(self):
        ta = self.provision('device-root-1')
        self.req('/api/file', {'path': 'd.txt', 'content': 'x'}, token=ta)
        root_files = self.req('/api/files')['files']  # master
        self.assertNotIn('d.txt', root_files)

    def test_health_reports_shared(self):
        self.assertTrue(self.req('/api/health')['shared'])

    def test_provider_key_injection(self):
        ta = self.provision('device-inj-1')
        self.req('/api/provider-keys',
                 {'name': 'Test', 'url': 'https://example.com/v1', 'key': 'sk-device-key'}, token=ta)
        profile = {'name': 'Test', 'kind': 'custom', 'baseUrl': 'https://example.com/v1',
                   'authMode': 'bearer', 'model': 'm'}
        r = self.req('/api/runs', {'prompt': 'hi', 'profile': profile, 'history': []}, token=ta)
        run = self.state.runs[r['id']]
        self.assertEqual(run.profile['apiKey'], 'sk-device-key')

    def test_run_without_key_is_actionable(self):
        ta = self.provision('device-nokey-1')
        profile = {'name': 'Test', 'kind': 'custom', 'baseUrl': 'https://example.com/v1',
                   'authMode': 'bearer', 'model': 'm'}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.req('/api/runs', {'prompt': 'hi', 'profile': profile, 'history': []}, token=ta)
        self.assertEqual(ctx.exception.code, 400)

    def test_per_device_run_limit(self):
        ta = self.provision('device-lim-a')
        tb = self.provision('device-lim-b')
        self.state.runs['fake1'] = types.SimpleNamespace(id='fake1', status='running', device='device-lim-a', prompt='x')
        profile = {'name': 'T', 'kind': 'custom', 'baseUrl': 'https://e.com', 'authMode': 'none', 'model': 'm'}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.req('/api/runs', {'prompt': 'hi', 'profile': profile, 'history': []}, token=ta)
        self.assertEqual(ctx.exception.code, 409)
        # another device is unaffected
        r = self.req('/api/runs', {'prompt': 'hi', 'profile': profile, 'history': []}, token=tb)
        self.assertIn('id', r)

    def test_global_run_cap(self):
        with patch.dict(os.environ, {'FORGE_MAX_RUNS': '1'}):
            ta = self.provision('device-cap-a')
            tb = self.provision('device-cap-b')
            self.state.runs['fake1'] = types.SimpleNamespace(id='fake1', status='running', device='device-cap-a', prompt='x')
            profile = {'name': 'T', 'kind': 'custom', 'baseUrl': 'https://e.com', 'authMode': 'none', 'model': 'm'}
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.req('/api/runs', {'prompt': 'hi', 'profile': profile, 'history': []}, token=tb)
            self.assertEqual(ctx.exception.code, 429)

    def test_github_token_is_per_device(self):
        ta = self.provision('device-gh-a')
        tb = self.provision('device-gh-b')
        with patch('runner.server.github.me', return_value='alice'):
            self.req('/api/github', {'token': 'x' * 40}, token=ta)
        self.assertTrue((self.state.workspace.root / 'devices' / 'device-gh-a' / 'github_token').is_file())
        self.assertFalse((self.state.workspace.root / 'devices' / 'device-gh-b' / 'github_token').exists())
        with patch('runner.server.github.me', return_value='alice'):
            ra = self.req('/api/github', token=ta)
        rb = self.req('/api/github', token=tb)
        self.assertTrue(ra['connected'])
        self.assertFalse(rb['connected'])
        self.assertIsNone(os.environ.get('GITHUB_TOKEN'))

    def test_single_user_mode_unchanged(self):
        # Without FORGE_SHARED the old single-token behavior holds.
        with patch.dict(os.environ, {'FORGE_SHARED': ''}):
            state = State(self.temp.name, self.master)
            self.assertFalse(state.device_tokens)
            self.assertEqual(state.workspace_for(None).root, state.workspace.root)
