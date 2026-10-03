"""python -m runner.server --workspace /path/to/project. Python 3.11+, no packages."""
import argparse
import base64
import hmac
import json
import mimetypes
import os
import re
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from .agent import Run
from . import github
from .connectors import Connector
from .skills import list_skills, save_skill, delete_skill, build_skill
from .workspace import Workspace, TOOLS
from .providers import discover_models, test_connection

WEB = Path(__file__).resolve().parents[1] / 'web'


class State:
    def __init__(self, root, token):
        self.workspace, self.token = Workspace(root), token
        self.runs, self.connectors, self.lock = {}, [], threading.RLock()
        self.device_tokens, self.device_workspaces = {}, {}  # shared mode: token -> device id
        if shared_mode():
            self._load_device_tokens()

    def active(self, device=None):
        return any(r.status in ('running', 'approval') and (device is None or getattr(r, 'device', None) == device)
                   for r in self.runs.values())

    def _load_device_tokens(self):
        root = _devices_root(self.workspace)
        if not root.is_dir():
            return
        for d in root.iterdir():
            token_file = d / 'token'
            if d.is_dir() and token_file.is_file():
                try:
                    tok = token_file.read_text(encoding='utf-8').strip()
                    if tok:
                        self.device_tokens[tok] = d.name
                except OSError:
                    pass

    def workspace_for(self, device):
        # In shared mode each device gets an isolated workspace directory.
        if device is None:
            return self.workspace
        if device not in self.device_workspaces:
            self.device_workspaces[device] = Workspace(str(_devices_root(self.workspace) / device))
        return self.device_workspaces[device]


def shared_mode():
    return os.environ.get('FORGE_SHARED') == '1'


def _devices_root(workspace):
    return Path(workspace.root) / 'devices'


def _device_id_valid(device):
    return isinstance(device, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,64}', device) is not None


def find_provider_key(workspace, url):
    for p in read_provider_keys(workspace):
        if p.get('url') == (url or '').strip():
            return p.get('key', '')
    return ''


def _api_keys_path(workspace):
    # Provider keys the user entered in the app, backed up on the runner so a
    # reinstall or a new device can restore them. Lives on the runner's own
    # storage (e.g. the Railway volume at /data/api.json).
    return Path(workspace.root) / 'api.json'


def read_provider_keys(workspace):
    try:
        data = json.loads(_api_keys_path(workspace).read_text(encoding='utf-8'))
        providers = data.get('providers', [])
        return [p for p in providers if isinstance(p, dict) and p.get('url') and p.get('key')]
    except (OSError, ValueError):
        return []


def save_provider_key(workspace, name, url, key):
    import time as _time
    providers = [p for p in read_provider_keys(workspace) if p.get('url') != url]
    providers.append({'name': name, 'url': url, 'key': key, 'updated_at': int(_time.time())})
    _api_keys_path(workspace).write_text(json.dumps({'providers': providers}, indent=2), encoding='utf-8')


def handler(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'Forge/0.5'

        def log_message(self, *args):
            pass  # Request bodies and credentials are never logged.

        def respond(self, code, value, mime='application/json'):
            raw = json.dumps(value).encode() if mime == 'application/json' else value
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.dispatch('GET')

        def do_POST(self):
            self.dispatch('POST')

        def dispatch(self, method):
            try:
                path = urlsplit(self.path).path
                if not path.startswith('/api/'):
                    allowed = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/cloud.js': 'cloud.js', '/shared.js': 'shared.js', '/style.css': 'style.css'}
                    if method != 'GET' or path not in allowed:
                        return self.respond(404, {'error': 'Not found'})
                    p = WEB / allowed[path]
                    if not p.is_file():
                        return self.respond(404, {'error': 'Not found'})
                    return self.respond(200, p.read_bytes(), mimetypes.guess_type(p)[0] or 'application/octet-stream')
                supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
                device, ws = None, state.workspace
                if shared_mode() and path == '/api/provision':
                    pass  # public: devices provision their own token silently on first launch
                elif hmac.compare_digest(supplied.encode(), state.token.encode()):
                    pass  # owner token: full access to the workspace root
                elif shared_mode() and supplied in state.device_tokens:
                    device = state.device_tokens[supplied]
                    ws = state.workspace_for(device)
                else:
                    return self.respond(401, {'error': 'Runner pairing expired or the pairing token is incorrect (HTTP 401). Reconnect the workspace in Cloud. This is not a provider API-key error.'})
                data = {}
                if method == 'POST':
                    if self.headers.get_content_type() != 'application/json':
                        return self.respond(415, {'error': 'Use application/json.'})
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 10_000_000:
                        return self.respond(413, {'error': 'Body must be 1 byte to 10 MB.'})
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError('JSON object required.')
                q = parse_qs(urlsplit(self.path).query)
                if shared_mode() and path == '/api/provision' and method == 'POST':
                    # Silent first-launch provisioning: the app sends a random device id,
                    # the backend mints a per-device token with an isolated workspace.
                    device_id = data.get('device', '')
                    if not _device_id_valid(device_id):
                        raise ValueError('device must be 8-64 characters: letters, numbers, dash, underscore.')
                    root = _devices_root(state.workspace)
                    token_file = root / device_id / 'token'
                    if token_file.is_file():
                        return self.respond(409, {'error': 'This device is already provisioned.'})
                    with state.lock:
                        count = sum(1 for d in root.iterdir()
                                    if d.is_dir() and (d / 'token').is_file()) if root.is_dir() else 0
                        if count >= int(os.environ.get('FORGE_MAX_DEVICES', '500')):
                            return self.respond(429, {'error': 'Device limit reached on this backend.'})
                        token_file.parent.mkdir(parents=True, exist_ok=True)
                        token = secrets.token_urlsafe(32)
                        token_file.write_text(token, encoding='utf-8')
                        try:
                            os.chmod(token_file, 0o600)
                        except OSError:
                            pass
                        state.device_tokens[token] = device_id
                    return self.respond(200, {'token': token})
                if path == '/api/health' and method == 'GET':
                    return self.respond(200, {'name': 'Forge Agent', 'workspace': ws.root.name,
                        'cliEnabled': os.environ.get('FORGE_ENABLE_CLI') == '1', 'version': '0.5.0',
                        'shared': shared_mode(),
                        'tools': [t['name'] for t in TOOLS]})
                if path == '/api/providers/discover' and method == 'POST':
                    return self.respond(200, discover_models(data))
                if path == '/api/providers/test' and method == 'POST':
                    return self.respond(200, test_connection(data))
                if path == '/api/skills' and method == 'GET':
                    return self.respond(200, {'skills': list_skills()})
                if path == '/api/skills' and method == 'POST':
                    skill = data.get('skill')
                    if not isinstance(skill, dict):
                        raise ValueError('skill must be an object.')
                    return self.respond(200, {'skill': save_skill(skill, [t['name'] for t in TOOLS])})
                if path == '/api/skills/delete' and method == 'POST':
                    name = data.get('name', '')
                    if not isinstance(name, str) or not name:
                        raise ValueError('name is required.')
                    delete_skill(name)
                    return self.respond(200, {'ok': True})
                if path == '/api/skills/build' and method == 'POST':
                    goal = data.get('goal', '')
                    profile = data.get('profile')
                    if not isinstance(profile, dict):
                        raise ValueError('Select a provider first so the AI has a model to build with.')
                    return self.respond(200, {'skill': build_skill(profile, goal, [t['name'] for t in TOOLS])})
                if path == '/api/repos/fetch' and method == 'POST':
                    return self.respond(200, {'message': ws.fetch_repo(
                        data.get('repo', ''), data.get('ref', 'main') or 'main', data.get('dest', '') or None)})
                if path == '/api/files' and method == 'GET':
                    return self.respond(200, {'files': ws.files()})
                if path == '/api/file' and method == 'GET':
                    return self.respond(200, {'content': ws.read(q.get('path', [''])[0])})
                if path == '/api/file' and method == 'POST':
                    with state.lock:
                        if state.active(device):
                            return self.respond(409, {'error': 'Wait for the agent to finish before editing manually.'})
                        ws.diff(data['path'], data['content'])
                        result = ws.execute('write_file', data, threading.Event())
                    return self.respond(200, {'message': result})
                if path == '/api/uploads' and method == 'POST':
                    # User-initiated attachment upload (any file type, up to 5 MB).
                    # Stored under uploads/; the agent reads it with its file tools.
                    name = data.get('name', '')
                    content = data.get('content', '')
                    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.\- ]{0,100}', name.strip()):
                        raise ValueError('File name must be 1-101 characters: letters, digits, spaces, dots, dashes, underscores.')
                    if not isinstance(content, str) or not content:
                        raise ValueError('content must be base64.')
                    try:
                        raw = base64.b64decode(content, validate=True)
                    except Exception:
                        raise ValueError('content must be valid base64.')
                    if len(raw) > 5 * 1024 * 1024:
                        raise ValueError('File exceeds 5 MB.')
                    dest = ws.path('uploads/' + name.strip())
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(raw)
                    return self.respond(200, {'path': 'uploads/' + name.strip()})
                if path == '/api/github' and method == 'GET':
                    login = ''
                    if github.token(ws, silent=True):
                        try:
                            login = github.me(ws)
                        except Exception:
                            login = ''
                    return self.respond(200, {'connected': bool(login), 'login': login})
                if path == '/api/github' and method == 'POST':
                    tok = data.get('token', '')
                    if not isinstance(tok, str) or len(tok.strip()) < 20:
                        raise ValueError('Paste a GitHub personal access token (fine-grained, with Contents and Pull requests access).')
                    tok = tok.strip()
                    try:
                        login = github.me(None, tok)
                    except Exception:
                        raise
                    github.save_token(ws, tok, device)
                    return self.respond(200, {'login': login})
                if path == '/api/github/disconnect' and method == 'POST':
                    github.clear_token(ws, device)
                    return self.respond(200, {'ok': True})
                if path == '/api/provider-keys' and method == 'GET':
                    return self.respond(200, {'providers': read_provider_keys(ws)})
                if path == '/api/provider-keys' and method == 'POST':
                    name = data.get('name', '')
                    url = data.get('url', '')
                    key = data.get('key', '')
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError('name is required.')
                    if not isinstance(url, str) or not url.strip():
                        raise ValueError('url is required.')
                    if not isinstance(key, str) or not key.strip():
                        raise ValueError('key is required.')
                    save_provider_key(ws, name.strip(), url.strip(), key.strip())
                    return self.respond(200, {'ok': True})
                if path == '/api/connectors' and method == 'GET':
                    return self.respond(200, {'connectors': [{'name': c.name, 'url': c.url,
                        'tools': [t['name'] for t in c.tools]} for c in state.connectors]})
                if path == '/api/connectors' and method == 'POST':
                    name = str(data.get('name', '')).strip()[:60]
                    if not name:
                        raise ValueError('Connector name is required.')
                    c = Connector(name, data['url'], data.get('token', '')).connect()
                    with state.lock:
                        if len(state.connectors) >= 4:
                            raise ValueError('Maximum four connectors. Remove one first.')
                        if sum(len(x.tools) for x in state.connectors) + len(c.tools) > 120:
                            raise ValueError('Maximum 120 connector tools across all connections.')
                        state.connectors.append(c)
                    return self.respond(200, {'name': c.name, 'count': len(c.tools)})
                if path == '/api/connectors/remove' and method == 'POST':
                    with state.lock:
                        if state.active(device):
                            return self.respond(409, {'error': 'Stop the active run before removing connectors.'})
                        index = int(data['index'])
                        if index < 0 or index >= len(state.connectors):
                            raise ValueError('Unknown connector.')
                        state.connectors.pop(index)
                    return self.respond(200, {'ok': True})
                if path == '/api/runs' and method == 'GET':
                    with state.lock:
                        runs = [{'id': r.id, 'prompt': r.prompt[:100], 'status': r.status,
                                 'chatId': r.chat_id}
                                for r in state.runs.values()
                                if device is None or getattr(r, 'device', None) == device]
                    return self.respond(200, {'runs': runs})
                if path == '/api/runs' and method == 'POST':
                    prompt = data.get('prompt', '')
                    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 30000:
                        raise ValueError('Task must contain 1–30000 characters.')
                    profile = data.get('profile', {})
                    if not isinstance(profile, dict):
                        raise ValueError('Provider profile must be an object.')
                    if (not profile.get('apiKey')
                            and profile.get('authMode') != 'none'
                            and profile.get('kind') != 'ollama'):
                        # Keys live on the backend (api.json); the app sends them once at setup.
                        injected = find_provider_key(ws, profile.get('baseUrl', ''))
                        if injected:
                            profile['apiKey'] = injected
                        else:
                            raise ValueError('No API key on the runner for this provider. Add it in the model setup first.')
                    history = data.get('history', [])
                    if not isinstance(history, list) or any(not isinstance(m, dict) for m in history):
                        raise ValueError('History must be a list of messages.')
                    skills = data.get('skills', [])
                    if not isinstance(skills, list) or any(not isinstance(s, str) for s in skills):
                        raise ValueError('skills must be a list of skill names.')
                    with state.lock:
                        if state.active(device):
                            return self.respond(409, {'error': 'One agent run at a time. Stop or finish the active task.'})
                        if shared_mode():
                            max_runs = int(os.environ.get('FORGE_MAX_RUNS', '4'))
                            busy = sum(1 for r in state.runs.values() if r.status in ('running', 'approval'))
                            if busy >= max_runs:
                                return self.respond(429, {'error': 'The shared backend is busy. Try again in a minute.'})
                        if len(state.runs) >= 30:
                            state.runs.pop(next(iter(state.runs)))
                        run = Run(ws, profile, prompt, state.connectors, history, skills)
                        run.device = device
                        chat_id = data.get('chatId')
                        run.chat_id = chat_id if isinstance(chat_id, str) and chat_id else None
                        state.runs[run.id] = run
                        threading.Thread(target=run.work, daemon=True).start()
                    return self.respond(201, {'id': run.id})
                parts = path.strip('/').split('/')
                if len(parts) in (3, 4) and parts[:2] == ['api', 'runs']:
                    run = state.runs.get(parts[2])
                    if run is None:
                        return self.respond(404, {'error': 'Run expired or runner restarted.'})
                    if method == 'GET' and len(parts) == 3:
                        return self.respond(200, run.snapshot())
                    if method == 'POST' and len(parts) == 4:
                        if parts[3] == 'cancel':
                            run.cancelled.set()
                            return self.respond(200, {'ok': True})
                        if parts[3] == 'approve':
                            if not isinstance(data.get('allow'), bool):
                                raise ValueError('allow must be a boolean.')
                            run.decide(data['id'], data['allow'])
                            return self.respond(200, {'ok': True})
                self.respond(404, {'error': 'Not found'})
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.respond(400, {'error': str(exc)[:400]})
            except Exception:
                self.respond(500, {'error': 'Runner error. Check configuration and request format.'})
    return Handler


def main():
    p = argparse.ArgumentParser(description='Forge personal coding runner')
    p.add_argument('--workspace', required=True)
    p.add_argument('--host', default='127.0.0.1')
    # Railway and similar hosts inject $PORT; default to 8787 for local runs.
    p.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8787')))
    p.add_argument('--cert', help='TLS certificate path')
    p.add_argument('--key', help='TLS private key path')
    args = p.parse_args()
    token = os.environ.get('FORGE_TOKEN') or secrets.token_urlsafe(32)
    if len(token) < 24:
        p.error('FORGE_TOKEN must be at least 24 characters.')
    server = ThreadingHTTPServer((args.host, args.port), handler(State(args.workspace, token)))
    if args.cert and args.key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    elif args.cert or args.key:
        p.error('Provide both --cert and --key.')
    print(f'Forge runner: {"https" if args.cert else "http"}://{args.host}:{args.port}', flush=True)
    print(f'Pairing token: {token}', flush=True)
    print('Use HTTPS or a trusted private network. Runner commands execute as this OS user.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
