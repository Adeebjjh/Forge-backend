"""Remote MCP Streamable HTTP client: JSON and POST SSE responses, token auth."""
import json
import threading
import urllib.request
import urllib.error
from .providers import validate_url, NoRedirect


class Connector:
    def __init__(self, name, url, token=''):
        self.name, self.url, self.token = name, validate_url(url), token
        self.session, self.version, self.counter = '', '2025-06-18', 0
        self.lock = threading.Lock()
        self.tools = []

    def rpc(self, method, params, notification=False):
        with self.lock:
            self.counter += 1
            body = {'jsonrpc': '2.0', 'method': method, 'params': params}
            if not notification:
                body['id'] = self.counter
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
            if self.session:
                headers['Mcp-Session-Id'] = self.session
            if method != 'initialize':
                headers['MCP-Protocol-Version'] = self.version
            req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as response:
                    if response.headers.get('Mcp-Session-Id'):
                        self.session = response.headers['Mcp-Session-Id']
                    if notification:
                        return {}
                    if 'text/event-stream' in response.headers.get('Content-Type', ''):
                        parts, total, result = [], 0, None
                        while True:
                            raw = response.readline(1_000_001)
                            if not raw:
                                break
                            total += len(raw)
                            if total > 2_000_000:
                                raise ValueError('MCP response exceeded limit.')
                            line = raw.decode('utf-8').rstrip('\r\n')
                            if line.startswith('data:'):
                                parts.append(line[5:].lstrip())
                            elif not line and parts:
                                item = json.loads('\n'.join(parts))
                                parts = []
                                if item.get('id') == body['id']:
                                    result = item
                                    break
                        if result is None:
                            raise ValueError('MCP stream ended without a matching response.')
                    else:
                        raw = response.read(2_000_001)
                        if len(raw) > 2_000_000:
                            raise ValueError('MCP response exceeded limit.')
                        result = json.loads(raw)
            except urllib.error.HTTPError as e:
                raise ValueError(f'MCP HTTP {e.code}. Check URL/token; OAuth-only servers need an external token.') from None
            except urllib.error.URLError:
                raise ValueError('Cannot reach MCP server.') from None
            if result.get('id') != body['id'] or 'error' in result:
                raise ValueError('MCP returned an error or mismatched response ID.')
            return result.get('result', {})

    def connect(self):
        init = self.rpc('initialize', {'protocolVersion': self.version, 'capabilities': {},
                        'clientInfo': {'name': 'forge-agent', 'version': '0.1.0'}})
        self.version = init.get('protocolVersion', self.version)
        if self.version not in ('2025-03-26', '2025-06-18', '2025-11-25'):
            raise ValueError('MCP server selected an unsupported protocol version.')
        self.rpc('notifications/initialized', {}, notification=True)
        cursor = None
        for _ in range(10):
            result = self.rpc('tools/list', {'cursor': cursor} if cursor else {})
            self.tools.extend(result.get('tools', []))
            cursor = result.get('nextCursor')
            if not cursor:
                break
        if len(self.tools) > 128:
            raise ValueError('Connector exposes too many tools (maximum 128).')
        return self

    def call(self, name, args):
        return self.rpc('tools/call', {'name': name, 'arguments': args})
