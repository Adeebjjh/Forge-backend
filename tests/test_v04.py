"""Forge 0.4 regressions: Gemini/Azure/custom-header protocols, run_python,
fetch_repo, and the AI skill builder."""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from runner import skills as skill_module
from runner.agent import Run
from runner.providers import Provider, auth_headers, discover_models, normalize_base, test_connection
from runner.server import State, handler
from runner.workspace import Workspace, TOOLS


def wait_for(predicate, seconds=5):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Condition did not become true')


class GeminiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def reply(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command == 'POST' else None
                cls.received.append((self.command, self.path, self.headers.get('x-goog-api-key'), body))
                if self.path == '/v1beta/models':
                    raw = json.dumps({'models': [{'name': 'models/gemini-2.0-flash', 'displayName': 'Gemini 2.0 Flash'}]}).encode()
                elif self.path.endswith(':generateContent'):
                    assert body['system_instruction']['parts'][0]['text'].startswith('You are a coding agent')
                    raw = json.dumps({'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]}).encode()
                else:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

            def do_GET(self): self.reply()

            def do_POST(self): self.reply()

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def profile(self, **kw):
        return {'kind': 'gemini', 'baseUrl': self.base, 'model': 'gemini-2.0-flash',
                'apiKey': 'test-key', 'authMode': 'x-goog-api-key', **kw}

    def test_normalize_keeps_v1beta(self):
        self.assertEqual(normalize_base('https://generativelanguage.googleapis.com', 'gemini'),
                         'https://generativelanguage.googleapis.com')
        self.assertEqual(normalize_base(self.base + '/v1beta/models/gemini-2.0-flash:generateContent', 'gemini'),
                         self.base + '/v1beta/models')

    def test_discovery_and_chat(self):
        result = discover_models(self.profile())
        self.assertEqual(result['models'], [{'id': 'gemini-2.0-flash', 'name': 'Gemini 2.0 Flash'}])
        self.assertEqual(result['protocol'], 'gemini')
        self.assertEqual(self.received[0][2], 'test-key')
        out = test_connection(self.profile())
        self.assertTrue(out['ok'])
        self.assertTrue(self.received[-1][1].endswith('gemini-2.0-flash:generateContent'))

    def test_tool_call_roundtrip(self):
        received = []

        class ToolHandler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(body)
                calls = body['contents'][-1]['parts']
                if any('functionResponse' in p for p in calls):
                    raw = json.dumps({'candidates': [{'content': {'parts': [{'text': 'done'}]}}]}).encode()
                else:
                    decls = body['tools'][0]['functionDeclarations']
                    assert decls[0]['name'] == 'list_files'
                    raw = json.dumps({'candidates': [{'content': {'parts': [
                        {'functionCall': {'name': 'list_files', 'args': {'path': '.'}}}]}}]}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

        server = ThreadingHTTPServer(('127.0.0.1', 0), ToolHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            provider = Provider(self.profile(baseUrl=base), [{'role': 'user', 'content': 'list the files'}])
            text, calls = provider.turn(TOOLS)
            self.assertEqual(calls[0]['name'], 'list_files')
            provider.results([(calls[0], 'a.py')])
            text, calls = provider.turn(TOOLS)
            self.assertEqual(text, 'done')
            self.assertEqual(calls, [])
        finally:
            server.shutdown(); server.server_close()


class AzureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def reply(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command == 'POST' else None
                cls.received.append((self.command, self.path, self.headers.get('api-key'), body))
                if self.path.startswith('/openai/deployments?'):
                    raw = json.dumps({'value': [{'name': 'gpt-4o-mini', 'model': 'gpt-4o-mini'}]}).encode()
                elif '/chat/completions' in self.path:
                    assert 'api-version=' in self.path
                    assert body['messages'][0]['role'] == 'system'
                    raw = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}).encode()
                else:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

            def do_GET(self): self.reply()

            def do_POST(self): self.reply()

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def profile(self, **kw):
        return {'kind': 'azure', 'baseUrl': self.base, 'model': 'gpt-4o-mini',
                'apiKey': 'test-key', 'authMode': 'api-key-header', **kw}

    def test_normalize_strips_openai_path(self):
        self.assertEqual(normalize_base(self.base + '/openai/deployments/x/chat/completions', 'azure'), self.base)

    def test_discovery_and_chat(self):
        result = discover_models(self.profile())
        self.assertEqual(result['models'], [{'id': 'gpt-4o-mini', 'name': 'gpt-4o-mini'}])
        self.assertEqual(self.received[0][2], 'test-key')
        out = test_connection(self.profile())
        self.assertTrue(out['ok'])
        self.assertIn('/openai/deployments/gpt-4o-mini/chat/completions', self.received[-1][1])


class CustomHeaderTests(unittest.TestCase):
    def test_custom_header_mode(self):
        headers = auth_headers('test-key', 'custom', 'custom', 'X-Gateway-Key')
        self.assertEqual(headers['X-Gateway-Key'], 'test-key')
        self.assertNotIn('Authorization', headers)

    def test_custom_header_name_validated(self):
        with self.assertRaises(ValueError):
            auth_headers('k', 'custom', 'custom', 'Bad Header!')
        with self.assertRaises(ValueError):
            auth_headers('k', 'custom', 'custom', '')

    def test_auto_modes(self):
        self.assertIn('x-goog-api-key', auth_headers('k', 'gemini', 'auto'))
        self.assertIn('api-key', auth_headers('k', 'azure', 'auto'))
        self.assertIn('x-api-key', auth_headers('k', 'anthropic', 'auto'))
        self.assertIn('Authorization', auth_headers('k', 'custom', 'auto'))


class RunPythonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Workspace(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_stdout_and_result(self):
        stop = threading.Event()
        out = self.ws.execute('run_python', {'code': 'print(6 * 7)'}, stop)
        self.assertIn('42', out)
        self.assertTrue(out.startswith('ok'))

    def test_error_reported(self):
        stop = threading.Event()
        out = self.ws.execute('run_python', {'code': 'raise RuntimeError("boom")'}, stop)
        self.assertTrue(out.startswith('error'))
        self.assertIn('RuntimeError', out)
        self.assertNotIn('Traceback', out)

    def test_timeout(self):
        stop = threading.Event()
        with self.assertRaises(ValueError):
            self.ws.execute('run_python', {'code': 'import time\ntime.sleep(30)', 'timeout': 1}, stop)

    def test_tool_registered(self):
        self.assertIn('run_python', [t['name'] for t in TOOLS])
        self.assertIn('fetch_repo', [t['name'] for t in TOOLS])


class FetchRepoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Workspace(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_zip(self, files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            for name, content in files.items():
                z.writestr(name, content)
        return buf.getvalue()

    def fake_urlopen(self, payload):
        class FakeResponse:
            status = 200

            def __enter__(self): return self

            def __exit__(self, *a): return False

            def read(self, n=-1): return payload

        return lambda req, timeout=None: FakeResponse()

    def test_fetch_extracts(self):
        payload = self.make_zip({'repo-main/README.md': '# hi', 'repo-main/src/a.py': 'x=1'})
        with patch('urllib.request.urlopen', self.fake_urlopen(payload)):
            message = self.ws.fetch_repo('owner/repo', 'main', 'dest')
        self.assertIn('owner/repo@main', message)
        self.assertEqual((Path(self.temp.name) / 'dest' / 'README.md').read_text(), '# hi')
        self.assertEqual((Path(self.temp.name) / 'dest' / 'src' / 'a.py').read_text(), 'x=1')

    def test_zip_slip_blocked(self):
        payload = self.make_zip({'repo-main/../../evil.py': 'x'})
        with patch('urllib.request.urlopen', self.fake_urlopen(payload)):
            with self.assertRaises(ValueError):
                self.ws.fetch_repo('owner/repo', 'main', 'dest2')

    def test_bad_repo_rejected(self):
        with self.assertRaises(ValueError):
            self.ws.fetch_repo('not a repo!!', 'main', None)

    def test_404_explained(self):
        def raising(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, 'nf', {}, None)

        with patch('urllib.request.urlopen', raising):
            with self.assertRaises(ValueError) as ctx:
                self.ws.fetch_repo('owner/missing', 'main', None)
        self.assertIn('not found', str(ctx.exception))


class SkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict(os.environ, {'FORGE_SKILLS_DIR': self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_crud(self):
        before = [s['name'] for s in skill_module.list_skills()]
        self.assertNotIn('Code Reviewer', before)
        self.assertIn('Code Review', before)  # builtin skill ships with the runner
        saved = skill_module.save_skill({'name': 'Code Reviewer', 'description': 'Reviews code.',
                                         'instructions': 'Review carefully.', 'tools': ['read_file']},
                                        [t['name'] for t in TOOLS])
        self.assertEqual(saved['name'], 'Code Reviewer')
        self.assertEqual(len(skill_module.list_skills()), len(before) + 1)
        self.assertEqual(skill_module.get_skill('Code Reviewer')['description'], 'Reviews code.')
        skill_module.save_skill({'name': 'Code Reviewer', 'description': 'Updated.',
                                 'instructions': 'Review.', 'tools': []}, [])
        self.assertEqual(len(skill_module.list_skills()), len(before) + 1)
        skill_module.delete_skill('Code Reviewer')
        self.assertEqual([s['name'] for s in skill_module.list_skills()], before)

    def test_validation(self):
        with self.assertRaises(ValueError):
            skill_module.save_skill({'name': 'x', 'description': 'd', 'instructions': 'i'}, [])
        with self.assertRaises(ValueError):
            skill_module.save_skill({'name': 'Valid Name', 'description': '', 'instructions': 'i'}, [])
        with self.assertRaises(ValueError):
            skill_module.save_skill({'name': 'Valid Name', 'description': 'd', 'instructions': 'i',
                                     'tools': ['nope']}, ['read_file'])

    def test_build_skill_parses_model_json(self):
        definition = json.dumps({'name': 'Doc Writer', 'description': 'Writes docs.',
                                 'instructions': 'Write clear docs.', 'tools': ['read_file']})
        with patch.object(Provider, 'turn', return_value=('```json\n' + definition + '\n```', [])):
            skill = skill_module.build_skill({'kind': 'custom', 'baseUrl': 'http://127.0.0.1:1',
                                              'model': 'm', 'apiKey': 'k'}, 'write docs', ['read_file'])
        self.assertEqual(skill['name'], 'Doc Writer')

    def test_build_skill_rejects_garbage(self):
        with patch.object(Provider, 'turn', return_value=('not json at all', [])):
            with self.assertRaises(ValueError):
                skill_module.build_skill({'kind': 'custom', 'baseUrl': 'http://127.0.0.1:1',
                                          'model': 'm', 'apiKey': 'k'}, 'write docs', [])


class ServerSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict(os.environ, {'FORGE_SKILLS_DIR': str(Path(self.temp.name) / 'skills')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = 'test-token-long-enough-for-pairing'
        self.state = State(self.temp.name, self.token)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def req(self, path, body=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.url + path,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
        with urllib.request.urlopen(request) as r:
            return json.loads(r.read())

    def test_skill_endpoints(self):
        skills = self.req('/api/skills')['skills']
        self.assertNotIn('Helper', [s['name'] for s in skills])
        self.assertIn('Code Review', [s['name'] for s in skills])  # builtin
        saved = self.req('/api/skills', {'skill': {'name': 'Helper', 'description': 'Helps.',
                                                   'instructions': 'Be helpful.', 'tools': []}})
        self.assertEqual(saved['skill']['name'], 'Helper')
        skills = self.req('/api/skills')['skills']
        self.assertIn('Helper', [s['name'] for s in skills])
        self.req('/api/skills/delete', {'name': 'Helper'})
        skills = self.req('/api/skills')['skills']
        self.assertNotIn('Helper', [s['name'] for s in skills])

    def test_build_endpoint(self):
        with patch('runner.server.build_skill', return_value={'name': 'X', 'description': 'd',
                                                              'instructions': 'i', 'tools': []}):
            r = self.req('/api/skills/build', {'goal': 'do x', 'profile': {'kind': 'custom'}})
        self.assertEqual(r['skill']['name'], 'X')

    def test_fetch_endpoint(self):
        with patch.object(Workspace, 'fetch_repo', return_value='Fetched ok') as m:
            r = self.req('/api/repos/fetch', {'repo': 'o/r', 'ref': 'main', 'dest': 'd'})
        self.assertEqual(r['message'], 'Fetched ok')
        m.assert_called_once_with('o/r', 'main', 'd')

    def test_run_with_skills(self):
        skill_module.save_skill({'name': 'Helper', 'description': 'Helps.',
                                 'instructions': 'Always be concise.', 'tools': []}, [])
        with patch.object(Provider, 'turn', return_value=('hi', [])):
            result = self.req('/api/runs', {'prompt': 'hello', 'profile': {'kind': 'ollama', 'model': 't'},
                                            'skills': ['Helper']})
            wait_for(lambda: self.state.runs[result['id']].status == 'done')
        run = self.state.runs[result['id']]
        self.assertIn('Always be concise', run.system_extra)

    def test_run_rejects_unknown_skills(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.req('/api/runs', {'prompt': 'hello', 'profile': {'kind': 'ollama', 'model': 't'},
                                   'skills': ['Missing']})
        self.assertEqual(ctx.exception.code, 400)

    def test_health_reports_tools_and_version(self):
        health = self.req('/api/health')
        self.assertEqual(health['version'], '0.5.0')
        self.assertIn('run_python', health['tools'])
        self.assertIn('fetch_repo', health['tools'])

    def test_github_endpoints(self):
        self.addCleanup(os.environ.pop, 'GITHUB_TOKEN', None)
        r = self.req('/api/github')
        self.assertFalse(r['connected'])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.req('/api/github', {'token': 'short'})
        self.assertEqual(ctx.exception.code, 400)
        with patch('runner.server.github.me', return_value='octocat'):
            r = self.req('/api/github', {'token': 'x' * 40})
        self.assertEqual(r['login'], 'octocat')
        with patch('runner.server.github.me', return_value='octocat'):
            r = self.req('/api/github')
        self.assertTrue(r['connected'])
        self.assertEqual(r['login'], 'octocat')
        self.req('/api/github/disconnect', {})
        r = self.req('/api/github')
        self.assertFalse(r['connected'])

    def test_provider_keys_backup(self):
        r = self.req('/api/provider-keys')
        self.assertEqual(r['providers'], [])
        self.req('/api/provider-keys', {'name': 'OpenAI', 'url': 'https://api.openai.com/v1', 'key': 'sk-test'})
        r = self.req('/api/provider-keys')
        self.assertEqual(len(r['providers']), 1)
        self.assertEqual(r['providers'][0]['url'], 'https://api.openai.com/v1')
        self.assertEqual(r['providers'][0]['key'], 'sk-test')
        self.req('/api/provider-keys', {'name': 'OpenAI', 'url': 'https://api.openai.com/v1', 'key': 'sk-new'})
        r = self.req('/api/provider-keys')
        self.assertEqual(len(r['providers']), 1)
        self.assertEqual(r['providers'][0]['key'], 'sk-new')
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.req('/api/provider-keys', {'name': '', 'url': 'x', 'key': 'y'})
        self.assertEqual(ctx.exception.code, 400)

    def test_upload_endpoint(self):
        import base64
        raw = b'\x50\x4b\x03\x04binary-data'
        r = self.req('/api/uploads', {'name': 'a.zip', 'content': base64.b64encode(raw).decode()})
        self.assertEqual(r['path'], 'uploads/a.zip')
        self.assertEqual((Path(self.temp.name) / 'uploads' / 'a.zip').read_bytes(), raw)
        # text file also works
        r = self.req('/api/uploads', {'name': 'n.txt', 'content': base64.b64encode(b'hi').decode()})
        self.assertEqual(r['path'], 'uploads/n.txt')

    def test_upload_rejects_bad_input(self):
        import base64
        good = base64.b64encode(b'x').decode()
        for body in [{'name': '../evil', 'content': good},
                     {'name': '', 'content': good},
                     {'name': 'a.zip', 'content': 'not-base64!!'},
                     {'name': 'a.zip', 'content': ''}]:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.req('/api/uploads', body)
            self.assertEqual(ctx.exception.code, 400)


class DeviceServerTests(unittest.TestCase):
    def test_start_stop(self):
        from runner import device as device_module
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as sk:
            token = 'a' * 32
            info = device_module.start_device_server(ws, sk, token)
            try:
                self.assertTrue(device_module.device_running())
                req = urllib.request.Request(info['url'] + '/api/health',
                                             headers={'Authorization': 'Bearer ' + token})
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(json.loads(r.read())['version'], '0.5.0')
            finally:
                self.assertTrue(device_module.stop_device_server())
                self.assertFalse(device_module.device_running())

    def test_double_start_rejected(self):
        from runner import device as device_module
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as sk:
            token = 'b' * 32
            device_module.start_device_server(ws, sk, token)
            try:
                with self.assertRaises(ValueError):
                    device_module.start_device_server(ws, sk, token)
            finally:
                device_module.stop_device_server()


if __name__ == '__main__':
    unittest.main()
