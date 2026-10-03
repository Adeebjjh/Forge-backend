import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from runner.providers import Provider, validate_url, request_json
from runner.workspace import Workspace, TOOLS, command, child_env
from runner.agent import Run
from runner.connectors import Connector
from runner.server import State, handler


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Condition did not become true')


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ws = Workspace(self.root)
        (self.root / 'hello.py').write_text('print("hello")\n')
    def tearDown(self):
        self.temp.cleanup()
    def test_traversal_and_secrets_blocked(self):
        for p in ('../outside', '/etc/passwd', '.env', '.env.local', '.ssh/id_rsa', 'server.pem'):
            with self.subTest(p=p), self.assertRaises(ValueError):
                self.ws.path(p)
    def test_symlink_escape_blocked(self):
        with tempfile.TemporaryDirectory() as other:
            (self.root / 'link').symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.ws.path('link/file.txt')
    def test_read_search_write(self):
        stop = threading.Event()
        self.assertIn('hello.py', self.ws.files())
        self.assertIn('hello.py:1:', self.ws.execute('search_files', {'query': 'hello'}, stop))
        diff = self.ws.diff('src/new.py', 'x = 1\n')
        self.assertIn('+x = 1', diff)
        self.ws.execute('write_file', {'path': 'src/new.py', 'content': 'x = 1\n'}, stop)
        self.assertEqual(self.ws.read('src/new.py'), 'x = 1\n')
    def test_large_file_blocked(self):
        (self.root / 'large.txt').write_text('a' * 300001)
        with self.assertRaises(ValueError): self.ws.read('large.txt')
    def test_subprocess_does_not_inherit_provider_keys(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'hidden-secret', 'FORGE_TOKEN': 'hidden-token'}):
            r = command([sys.executable, '-c', 'import os; print(os.environ.get("OPENAI_API_KEY", "absent")); print(os.environ.get("FORGE_TOKEN", "absent"))'], self.root, threading.Event())
        self.assertIn('absent\nabsent', r)
        self.assertNotIn('hidden', r)
    def test_command_timeout(self):
        start = time.monotonic()
        r = command([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, threading.Event(), 1)
        self.assertIn('timeout', r)
        self.assertLess(time.monotonic() - start, 4)
    def test_command_cancellation(self):
        cancel = threading.Event()
        threading.Timer(.1, cancel.set).start()
        r = command([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, cancel)
        self.assertIn('cancelled', r)


class ProviderTests(unittest.TestCase):
    def test_urls(self):
        for url in ('http://example.com/v1', 'file:///etc/passwd', 'https://key@example.com', 'https://example.com/?key=foo'):
            with self.subTest(url=url), self.assertRaises(ValueError): validate_url(url)
        self.assertEqual(validate_url('http://192.168.1.3:11434/'), 'http://192.168.1.3:11434')
    def test_tailscale_range_boundaries(self):
        for address in ('100.64.0.1', '100.100.100.100', '100.127.255.254'):
            self.assertEqual(validate_url('http://' + address + ':8787'), 'http://' + address + ':8787')
        for address in ('100.63.255.255', '100.128.0.0', '100.200.1.1'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_url('http://' + address + ':8787')
    def test_ollama_tool_roundtrip(self):
        p = Provider({'kind': 'ollama', 'model': 'test'}, [{'role': 'user', 'content': 'read'}])
        response = {'message': {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'read_file', 'arguments': {'path':'a.py'}}}]}}
        with patch('runner.providers.request_json', return_value=response) as req:
            _, calls = p.turn(TOOLS)
            p.results([(calls[0], 'content')])
        self.assertEqual(req.call_args.args[0], 'http://127.0.0.1:11434/api/chat')
        self.assertEqual(p.messages[-1]['tool_name'], 'read_file')
    def test_anthropic_tool_roundtrip(self):
        p = Provider({'kind': 'anthropic', 'model': 'test', 'apiKey': 'test-key'})
        response = {'content': [{'type':'text','text':'Inspecting'}, {'type':'tool_use','id':'t1','name':'read_file','input':{'path':'a.py'}}]}
        with patch('runner.providers.request_json', return_value=response) as req:
            text, calls = p.turn(TOOLS)
            p.results([(calls[0], 'file text')])
        self.assertEqual(text,'Inspecting')
        self.assertEqual(req.call_args.args[2]['x-api-key'], 'test-key')
        self.assertEqual(p.messages[-1]['content'][0]['tool_use_id'], 't1')
    def test_responses_preserves_reasoning(self):
        p = Provider({'kind':'responses','model':'test'})
        response = {'output':[{'type':'reasoning','id':'r1','summary':[]}, {'type':'function_call','call_id':'c1','name':'read_file','arguments':'{"path":"a.py"}'}]}
        with patch('runner.providers.request_json',return_value=response):
            _, calls = p.turn(TOOLS)
            p.results([(calls[0], 'contents')])
        self.assertEqual(p.messages[0]['type'], 'reasoning')
        self.assertEqual(p.messages[-1]['type'], 'function_call_output')
        self.assertEqual(p.messages[-1]['call_id'], 'c1')
    def test_custom_chat_tool_roundtrip(self):
        p = Provider({'kind':'custom','model':'test','baseUrl':'https://provider.example/v1'})
        response = {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'c1','type':'function','function':{'name':'read_file','arguments':'{"path":"a.py"}'}}]}}]}
        with patch('runner.providers.request_json',return_value=response) as req:
            _, calls = p.turn(TOOLS)
            p.results([(calls[0], 'contents')])
        self.assertEqual(p.messages[-1]['tool_call_id'],'c1')
        self.assertTrue(req.call_args.args[0].endswith('/v1/chat/completions'))


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Workspace(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def start_edit(self):
        run = Run(self.ws, {'kind':'ollama','model':'test','apiKey':'secret'}, 'write a file', [])
        responses = [('', [{'id':'1','name':'write_file','arguments':{'path':'new.py','content':'x=1\n'}}]), ('Done',[])]
        return run, responses
    def test_approve_edit(self):
        run, responses = self.start_edit()
        with patch.object(Provider,'turn',side_effect=responses):
            thread = threading.Thread(target=run.work); thread.start()
            wait_for(lambda: bool(run.snapshot()['pending']))
            self.assertFalse((self.ws.root/'new.py').exists())
            item=run.snapshot()['pending'][0]
            self.assertIn('+x=1',item['details'])
            run.decide(item['id'],True); thread.join(3)
        self.assertEqual(self.ws.read('new.py'),'x=1\n')
        self.assertEqual(run.status,'done')
        self.assertNotIn('apiKey',run.profile)
    def test_deny_edit(self):
        run,responses=self.start_edit()
        with patch.object(Provider,'turn',side_effect=responses):
            thread=threading.Thread(target=run.work);thread.start()
            wait_for(lambda: bool(run.snapshot()['pending']))
            run.decide(run.snapshot()['pending'][0]['id'],False);thread.join(3)
        self.assertFalse((self.ws.root/'new.py').exists())
        self.assertTrue(any('denied' in e['text'] for e in run.events))
    def test_cancel_approval(self):
        run,responses=self.start_edit()
        with patch.object(Provider,'turn',side_effect=responses):
            thread=threading.Thread(target=run.work);thread.start()
            wait_for(lambda: bool(run.snapshot()['pending']))
            run.cancelled.set();thread.join(3)
        self.assertEqual(run.status,'cancelled')
        self.assertFalse((self.ws.root/'new.py').exists())
    def test_auto_approve_skips_prompt(self):
        run=Run(self.ws,{'kind':'ollama','model':'test','apiKey':'secret','autoApprove':True},'write a file',[])
        self.assertTrue(run.auto_approve)
        self.assertTrue(run.approve('Edit new.py','details'))
        snap=run.snapshot()
        self.assertEqual(snap['pending'],[])
        self.assertTrue(any('Auto-approved' in e['text'] for e in snap['events']))
    def test_chat_id_in_snapshot(self):
        run=Run(self.ws,{'kind':'ollama','model':'test'},'hi',[])
        self.assertIsNone(run.snapshot()['chatId'])
        run.chat_id='chat-1'
        self.assertEqual(run.snapshot()['chatId'],'chat-1')
    def test_edit_changed_after_approval(self):
        run,responses=self.start_edit()
        with patch.object(Provider,'turn',side_effect=responses):
            thread=threading.Thread(target=run.work);thread.start()
            wait_for(lambda: bool(run.snapshot()['pending']))
            (self.ws.root/'new.py').write_text('user modification\n')
            run.decide(run.snapshot()['pending'][0]['id'],True);thread.join(3)
        self.assertEqual(self.ws.read('new.py'),'user modification\n')
    def test_cli_requires_explicit_run_approval(self):
        run=Run(self.ws,{'kind':'codex-cli','apiKey':'unit-test-key'},'inspect',[])
        with patch.dict(os.environ,{'FORGE_ENABLE_CLI':'1'}), patch('runner.agent.shutil.which',return_value='/bin/codex'), patch('runner.agent.command',return_value='exit=0\nDone') as cmd:
            thread=threading.Thread(target=run.work);thread.start()
            wait_for(lambda: bool(run.snapshot()['pending']))
            cmd.assert_not_called()
            run.decide(run.snapshot()['pending'][0]['id'],True);thread.join(3)
            argv=cmd.call_args.args[0]
            self.assertIn('workspace-write',argv)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',argv)
            self.assertEqual(cmd.call_args.kwargs['env']['FORGE_PROVIDER_KEY'],'unit-test-key')
    def test_connector_does_not_execute_without_approval(self):
        from unittest.mock import Mock
        connector=Mock()
        connector.name='test-tools'
        connector.tools=[{'name':'send_action','description':'Example action','inputSchema':{'type':'object','properties':{}}}]
        run=Run(self.ws,{'kind':'ollama','model':'test'},'use connector',[connector])
        turns=[('',[{'id':'1','name':'mcp_0_0','arguments':{}}]),('Finished',[])]
        with patch.object(Provider,'turn',side_effect=turns):
            thread=threading.Thread(target=run.work);thread.start()
            wait_for(lambda:bool(run.snapshot()['pending']))
            connector.call.assert_not_called()
            run.decide(run.snapshot()['pending'][0]['id'],False);thread.join(3)
        connector.call.assert_not_called()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.token='test-token-long-enough-for-pairing'
        self.state=State(self.temp.name,self.token)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.state))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.temp.cleanup()
    def req(self,path,body=None,auth=True):
        headers={'Authorization':'Bearer '+self.token} if auth else {}
        if body is not None: headers['Content-Type']='application/json'
        request=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with urllib.request.urlopen(request) as r: return json.loads(r.read())
    def test_auth_required(self):
        with self.assertRaises(urllib.error.HTTPError) as e: self.req('/api/health',auth=False)
        self.assertEqual(e.exception.code,401)
        self.assertEqual(self.req('/api/health')['name'],'Forge Agent')
    def test_file_write_and_read_api(self):
        self.req('/api/file',{'path':'a.py','content':'print(1)'})
        self.assertEqual(self.req('/api/file?path=a.py')['content'],'print(1)')
    def test_only_json_post(self):
        request=urllib.request.Request(self.url+'/api/runs',data=b'{}',headers={'Authorization':'Bearer '+self.token})
        with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(request)
        self.assertEqual(e.exception.code,415)
    def test_api_run_lifecycle(self):
        with patch.object(Provider,'turn',return_value=('Hello from test provider',[])):
            result=self.req('/api/runs',{'prompt':'hello','profile':{'kind':'ollama','model':'test'}})
            wait_for(lambda:self.state.runs[result['id']].status=='done')
        run=self.req('/api/runs/'+result['id'])
        self.assertEqual(run['events'][-1]['text'],'Hello from test provider')
    def test_run_chat_id_roundtrip(self):
        with patch.object(Provider,'turn',return_value=('hi',[])):
            result=self.req('/api/runs',{'prompt':'hello','profile':{'kind':'ollama','model':'test'},'chatId':'chat-9'})
            wait_for(lambda:self.state.runs[result['id']].status=='done')
        runs=self.req('/api/runs')['runs']
        match=[r for r in runs if r['id']==result['id']]
        self.assertEqual(len(match),1)
        self.assertEqual(match[0]['chatId'],'chat-9')
        self.assertEqual(self.req('/api/runs/'+result['id'])['chatId'],'chat-9')


class MCPTests(unittest.TestCase):
    def test_mcp_handshake_tool_call_json_and_sse(self):
        for sse in (False,True):
            with self.subTest(sse=sse):
                seen=[]
                class MockMCP(BaseHTTPRequestHandler):
                    def log_message(self,*a): pass
                    def do_POST(self):
                        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        seen.append((body,dict(self.headers)))
                        if 'id' not in body:
                            self.send_response(202);self.end_headers();return
                        results={'initialize':{'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'test','version':'1'}},
                                 'tools/list':{'tools':[{'name':'lookup','inputSchema':{'type':'object','properties':{}}}]},
                                 'tools/call':{'content':[{'type':'text','text':'found'}]}}
                        data=json.dumps({'jsonrpc':'2.0','id':body['id'],'result':results[body['method']]})
                        payload=('data: '+data+'\n\n' if sse else data).encode()
                        self.send_response(200);self.send_header('Content-Type','text/event-stream' if sse else 'application/json')
                        self.send_header('Mcp-Session-Id','session123');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
                server=ThreadingHTTPServer(('127.0.0.1',0),MockMCP)
                threading.Thread(target=server.serve_forever,daemon=True).start()
                try:
                    c=Connector('test',f'http://127.0.0.1:{server.server_port}/mcp','token').connect()
                    self.assertEqual(c.tools[0]['name'],'lookup')
                    self.assertEqual(c.call('lookup',{})['content'][0]['text'],'found')
                    self.assertEqual(seen[1][0]['method'],'notifications/initialized')
                    self.assertEqual(seen[-1][1]['Mcp-Session-Id'],'session123')
                    self.assertEqual(seen[-1][1]['Authorization'],'Bearer token')
                finally: server.shutdown();server.server_close()


if __name__=='__main__': unittest.main()
