"""Local HTTP regressions for discovery/chat authentication and response handling."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from runner.providers import Provider, auth_headers, normalize_base, normalize_key, discover_models, test_connection
from runner.agent import Run
from runner.workspace import Workspace


class ProviderHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.reply()
            def do_POST(self): self.reply()
            def reply(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command == 'POST' else None
                cls.received.append((self.command, self.path, dict(self.headers), body))
                status, mime, raw = 200, 'application/json', None
                if '/html/' in self.path: raw, mime = b'<html>Login to gateway</html>', 'text/html'
                elif '/empty/' in self.path: raw = b''
                elif '/malformed/' in self.path: raw = b'not json: do not expose private details'
                elif '/array/' in self.path: raw = b'[]'
                elif '/redirect/' in self.path:
                    self.send_response(302); self.send_header('Location','http://127.0.0.1:1/should-not-follow'); self.end_headers(); return
                elif '/forbidden/' in self.path: status, raw = 403, b'private response'
                elif '/limited/' in self.path: status, raw = 429, b'private response'
                elif '/badgateway/' in self.path: status, raw = 502, json.dumps({'error': 'Bad gateway at proxy'}).encode()
                elif '/unavailable/' in self.path: status, raw = 503, b'{}'
                elif '/gatewaytimeout/' in self.path: status, raw = 504, b'{}'
                elif '/jsonerror/' in self.path: status, raw = 401, json.dumps({'code':'INVALID_API_KEY','message':'Invalid API key'}).encode()
                elif '/longerror/' in self.path: status, raw = 401, json.dumps({'error':{'message':'x'*500}}).encode()
                elif '/err200/' in self.path: status, raw = 200, json.dumps({'error':{'message':'Insufficient quota for model X'}}).encode()
                elif self.command == 'POST':
                    header = 'x-api-key' if '/xkey/' in self.path else 'api-key' if '/apikey/' in self.path else 'Authorization'
                    expected = 'test-key' if header != 'Authorization' else 'Bearer test-key'
                    if '/public/' not in self.path and self.headers.get(header) != expected:
                        status, raw = 401, b'private echoed key: bad-secret'
                    elif self.path.endswith('/messages'):
                        raw = json.dumps({'content':[{'type':'text','text':'OK'}]}).encode()
                    elif self.path.endswith('/responses'):
                        raw = json.dumps({'output':[{'type':'message','content':[{'type':'output_text','text':'OK'}]}]}).encode()
                    elif self.path.endswith('/api/chat'):
                        raw = json.dumps({'message':{'role':'assistant','content':'OK'}}).encode()
                    else:
                        raw = json.dumps({'choices':[{'message':{'role':'assistant','content':'OK'}}]}).encode()
                else:
                    data = {'models':[{'name':'test-model'}]} if self.path.endswith('/api/tags') else {'data':[{'id':'test-model'}]}
                    raw = json.dumps(data).encode()
                    if '/bom/' in self.path: raw = b'\xef\xbb\xbf' + raw
                self.send_response(status); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        cls.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def profile(self, prefix='gateway/v2', **kw):
        return {'kind':'custom','baseUrl':self.base+'/'+prefix,'model':'test-model','apiKey':'test-key','authMode':'bearer',**kw}
    def test_discovery_probe_and_real_chat_share_url_and_credentials(self):
        for kind in ('anthropic','responses','custom','ollama'):
            for mode, prefix in (('bearer','gateway/v2'),('api-key','xkey/v2'),('api-key-header','apikey/v2'),('none','public/v2')):
                with self.subTest(kind=kind,mode=mode):
                    p = self.profile(prefix,kind=kind,apiKey='  Bearer test-key\n',authMode=mode)
                    start = len(self.received)
                    listed = discover_models(p)
                    self.assertFalse(listed['chatVerified'])
                    p['baseUrl'] = listed['baseUrl']
                    self.assertTrue(test_connection(p)['ok'])
                    provider = Provider(p,[{'role':'user','content':'Hi'}])
                    self.assertEqual(provider.turn([])[0], 'OK')
                    discovery, probe, chat = self.received[start:]
                    for h in ('Authorization','X-Api-Key','Api-Key','Anthropic-Version'):
                        self.assertEqual(discovery[2].get(h),probe[2].get(h))
                        self.assertEqual(probe[2].get(h),chat[2].get(h))
                    self.assertEqual(probe[1],chat[1])
                    self.assertTrue(chat[1].startswith('/'+prefix+'/'))
                    self.assertFalse(chat[3]['stream'])
    def test_public_models_do_not_mark_unauthorized_chat_as_working(self):
        p = self.profile(apiKey='bad-secret')
        self.assertEqual(discover_models(p)['models'][0]['id'],'test-model')
        with self.assertRaisesRegex(ValueError,'HTTP 401') as caught: test_connection(p)
        self.assertNotIn('bad-secret',str(caught.exception))
        self.assertNotIn('private echoed',str(caught.exception))
        with tempfile.TemporaryDirectory() as root:
            run = Run(Workspace(root),p,'Hi',[]); run.work()
            self.assertEqual(run.status,'error')
            self.assertIn('HTTP 401',run.events[-1]['text'])
            self.assertNotIn('apiKey',run.profile)
    def test_clear_non_json_and_http_errors(self):
        for prefix, message in [('html','HTML instead of JSON'),('empty','empty response'),('malformed','invalid JSON'),('array','unexpected JSON'),('redirect','redirected'),('forbidden','HTTP 403'),('limited','HTTP 429')]:
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError,message) as caught:
                discover_models(self.profile(prefix))
            self.assertNotIn('line 1',str(caught.exception))
            self.assertNotIn('private',str(caught.exception))
    def test_gateway_errors_say_the_key_was_not_rejected(self):
        for prefix, code_text in [('badgateway','HTTP 502 Bad Gateway'),('unavailable','HTTP 503'),('gatewaytimeout','HTTP 504')]:
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError,code_text) as caught:
                discover_models(self.profile(prefix,authMode='none'))
            if prefix == 'badgateway':
                self.assertIn('was not rejected',str(caught.exception))
                self.assertIn('Bad gateway at proxy',str(caught.exception))
    def test_provider_json_error_message_is_surfaced(self):
        with self.assertRaisesRegex(ValueError,'Invalid API key') as caught:
            discover_models(self.profile('jsonerror',authMode='none'))
        self.assertIn('HTTP 401',str(caught.exception))
    def test_200_error_payload_message_is_surfaced(self):
        with self.assertRaisesRegex(ValueError,'Insufficient quota for model X'):
            discover_models(self.profile('err200',authMode='none'))
    def test_long_provider_message_is_truncated(self):
        with self.assertRaises(ValueError) as caught:
            discover_models(self.profile('longerror',authMode='none'))
        self.assertNotIn('x'*201,str(caught.exception))
        self.assertIn('x'*200,str(caught.exception))
    def test_utf8_bom_is_accepted(self):
        self.assertEqual(discover_models(self.profile('bom'))['models'][0]['id'],'test-model')
    def test_unversioned_full_endpoint_survives_discovery_save_and_chat(self):
        p = self.profile('chat/completions',authMode='none')
        result = discover_models(p)
        self.assertEqual(result['baseUrl'],p['baseUrl'])
        self.assertEqual(normalize_base(result['baseUrl'],'custom'),self.base)


class ProviderConfigTests(unittest.TestCase):
    def test_custom_prefixes_and_versions_are_not_rewritten(self):
        for path in ('/api','/openai','/gateway/v2','/v1beta','/anthropic/v1'):
            self.assertEqual(normalize_base('https://example.com'+path,'custom'),'https://example.com'+path)
            self.assertEqual(normalize_base('https://example.com'+path+'/chat/completions','custom'),'https://example.com'+path)
        self.assertEqual(normalize_base('https://example.com','custom'),'https://example.com/v1')
    def test_key_cleanup_and_injection_rejection(self):
        self.assertEqual(normalize_key(' \nBearer test-key\r\n'),'test-key')
        self.assertEqual(auth_headers('Bearer test-key','custom')['Authorization'],'Bearer test-key')
        for key in ('key\r\nAuthorization: stolen','key with spaces','key\u200b'):
            with self.assertRaises(ValueError): normalize_key(key)
    def test_auth_mode_none_never_uses_environment_credentials(self):
        with patch.dict(os.environ,{'CUSTOM_API_KEY':'server-secret'}):
            for profile in ({'authMode':'none'},{'apiKey':''}):
                p = Provider({'kind':'custom','baseUrl':'https://example.com/v1','model':'test',**profile})
                self.assertNotIn('Authorization',p.headers)
    def test_bad_tool_json_has_actionable_error(self):
        p = Provider({'kind':'custom','baseUrl':'https://example.com','model':'test'})
        with patch('runner.providers.request_json',return_value={'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'x','function':{'name':'read_file','arguments':'not-json'}}]}}]}):
            with self.assertRaisesRegex(ValueError,'invalid tool arguments'): p.turn([])
    def test_wrong_protocol_does_not_leak_keyerror(self):
        p = Provider({'kind':'custom','baseUrl':'https://example.com','model':'test'})
        with patch('runner.providers.request_json',return_value={'content':[{'type':'text','text':'OK'}]}):
            with self.assertRaisesRegex(ValueError,'selected protocol'): p.turn([])
    def test_codex_auth_mode_matches_discovery(self):
        for mode,header in [('bearer','Authorization'),('api-key','x-api-key'),('api-key-header','api-key'),('none',None)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                profile = {'kind':'codex-cli','baseUrl':'https://example.com/gateway/v2','apiKey':' Bearer test-key ','authMode':mode}
                run = Run(Workspace(root),profile,'Hi',[])
                with patch.dict(os.environ,{'FORGE_ENABLE_CLI':'1'}), patch('runner.agent.shutil.which',return_value='/bin/codex'), patch.object(run,'approve',return_value=True), patch('runner.agent.command',return_value='exit=0\nDone') as cmd:
                    run.cli()
                argv = cmd.call_args.args[0]; env = cmd.call_args.kwargs['env']
                if header:
                    self.assertEqual(env['FORGE_PROVIDER_KEY'],'test-key')
                    setting = 'model_providers.forge.env_key="FORGE_PROVIDER_KEY"' if header == 'Authorization' else 'model_providers.forge.env_http_headers.'+header+'="FORGE_PROVIDER_KEY"'
                    self.assertIn(setting,argv)
                else:
                    self.assertNotIn('FORGE_PROVIDER_KEY',env)
                    self.assertFalse(any('env_key=' in a for a in argv))
                self.assertNotIn('test-key',' '.join(argv))
    def test_claude_bearer_key_uses_the_same_header(self):
        with tempfile.TemporaryDirectory() as root:
            run = Run(Workspace(root),{'kind':'claude-code','baseUrl':'https://example.com/anthropic/v1','apiKey':' Bearer test-key ','authMode':'bearer'},'Hi',[])
            with patch.dict(os.environ,{'FORGE_ENABLE_CLI':'1'}), patch('runner.agent.shutil.which',return_value='/bin/claude'), patch.object(run,'approve',return_value=True), patch('runner.agent.command',return_value='exit=0\nDone') as cmd:
                run.cli()
            env = cmd.call_args.kwargs['env']
            self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'],'test-key')
            self.assertEqual(env['ANTHROPIC_BASE_URL'],'https://example.com/anthropic')
            self.assertNotIn('ANTHROPIC_API_KEY',env)
