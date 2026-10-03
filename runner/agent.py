import copy
import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from .providers import Provider, auth_mode, normalize_base, profile_key
from .skills import get_skill
from .workspace import TOOLS, command, child_env


class Run:
    def __init__(self, workspace, profile, prompt, connectors, history=None, skills=None):
        self.id = secrets.token_hex(12)
        self.workspace, self.profile, self.prompt = workspace, dict(profile), prompt
        self.connectors, self.history = list(connectors), history or []
        self.skill_names = [s for s in (skills or []) if isinstance(s, str)][:8]
        self.system_extra = '\n\n'.join(
            f'Active skill "{s["name"]}": {s["description"]}\n{s["instructions"]}'
            for s in (get_skill(n) for n in self.skill_names) if s)
        if self.skill_names and not self.system_extra:
            raise ValueError('None of the selected skills exist on this runner.')
        self.events, self.pending = [], {}
        self.status = 'running'
        self.cancelled = threading.Event()
        self.lock = threading.RLock()
        self.auto_approve = bool(profile.get('autoApprove'))
        self.chat_id = None  # set by server.py from the app's chat id
        self.add('user', prompt)

    def add(self, kind, text, **extra):
        with self.lock:
            self.events.append({'type': kind, 'text': str(text)[:100_000], 'time': time.time(), **extra})

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({'id': self.id, 'status': self.status, 'chatId': self.chat_id,
                'events': self.events,
                'pending': [{k: v for k, v in p.items() if k not in ('event', 'decision')}
                            for p in self.pending.values()]})

    def approve(self, title, details):
        if self.auto_approve:
            self.add('approval', 'Auto-approved: ' + title)
            return True
        aid, wake = secrets.token_hex(8), threading.Event()
        with self.lock:
            self.pending[aid] = {'id': aid, 'title': title, 'details': details,
                                 'event': wake, 'decision': None}
            self.status = 'approval'
        deadline = time.monotonic() + 1800
        while not wake.wait(.2):
            if self.cancelled.is_set() or time.monotonic() >= deadline:
                break
        with self.lock:
            item = self.pending.pop(aid)
            ok = item['decision'] is True and not self.cancelled.is_set()
            self.status = 'running'
        self.add('approval', ('Approved: ' if ok else 'Denied / expired: ') + title)
        return ok

    def decide(self, aid, allowed):
        with self.lock:
            if aid not in self.pending or self.pending[aid]['decision'] is not None:
                raise ValueError('Approval is no longer pending.')
            self.pending[aid]['decision'] = allowed
            self.pending[aid]['event'].set()

    def cli(self):
        if os.environ.get('FORGE_ENABLE_CLI') != '1':
            raise ValueError('CLI mode is disabled. Install the CLI and start the runner with FORGE_ENABLE_CLI=1.')
        kind = self.profile['kind']
        exe = 'claude' if kind == 'claude-code' else 'codex'
        executable = shutil.which(exe)
        if not executable:
            raise ValueError(f'{exe} is not installed on the runner PATH.')
        prefix = [executable]
        if os.name == 'nt' and executable.lower().endswith(('.cmd', '.bat')):
            package = '@anthropic-ai/claude-code' if exe == 'claude' else '@openai/codex'
            pkg = Path(executable).parent / 'node_modules' / package
            manifest = json.loads((pkg / 'package.json').read_text(encoding='utf-8'))
            bins = manifest['bin']
            script = (pkg / (bins[exe] if isinstance(bins, dict) else bins)).resolve()
            if not script.is_relative_to(pkg.resolve()) or not shutil.which('node'):
                raise ValueError('CLI installation is invalid. Reinstall the official package.')
            prefix = [shutil.which('node'), str(script)]
        model = self.profile.get('model', '').strip()
        env = child_env()
        protocol = 'anthropic' if exe == 'claude' else 'responses'
        mode = auth_mode(protocol, self.profile.get('authMode', 'auto'))
        key = profile_key(self.profile, protocol)
        if exe == 'claude':
            if os.environ.get('CLAUDE_CODE_GIT_BASH_PATH'):
                env['CLAUDE_CODE_GIT_BASH_PATH'] = os.environ['CLAUDE_CODE_GIT_BASH_PATH']
            if mode not in ('api-key', 'bearer'):
                raise ValueError('Claude Code supports Bearer or x-api-key authentication here. Use the Claude-compatible API agent for other authentication modes.')
            credential = 'ANTHROPIC_AUTH_TOKEN' if mode == 'bearer' else 'ANTHROPIC_API_KEY'
            if key:
                env[credential] = key
            base = self.profile.get('baseUrl', '').strip()
            if base:
                normalized = normalize_base(base, 'anthropic')
                if not normalized.endswith('/v1'):
                    raise ValueError('Claude Code appends /v1/messages. Use a base ending in /v1, or choose the Claude-compatible API agent for this custom path.')
                env['ANTHROPIC_BASE_URL'] = normalized.removesuffix('/v1')
            argv = prefix + ['-p', '--output-format', 'json', '--permission-mode', 'default',
                    '--allowedTools', 'Read,Glob,Grep,Edit,Write,Bash', '--max-turns', '12']
        else:
            argv = prefix + ['exec', '--sandbox', 'workspace-write', '--json']
            base = self.profile.get('baseUrl', '').strip()
            base = normalize_base(base or 'https://api.openai.com/v1', 'responses')
            if key or mode == 'none' or base != 'https://api.openai.com/v1':
                config = {'model_provider': 'forge', 'model_providers.forge.name': 'Forge gateway',
                          'model_providers.forge.base_url': base, 'model_providers.forge.wire_api': 'responses',
                          'model_providers.forge.requires_openai_auth': False}
                if mode != 'none':
                    if not key:
                        raise ValueError('Enter the API key for this Codex gateway. Its model list may be public.')
                    env['FORGE_PROVIDER_KEY'] = key
                    if mode == 'bearer':
                        config['model_providers.forge.env_key'] = 'FORGE_PROVIDER_KEY'
                    else:
                        header = 'x-api-key' if mode == 'api-key' else 'api-key'
                        config['model_providers.forge.env_http_headers.' + header] = 'FORGE_PROVIDER_KEY'
                for field, value in config.items():
                    argv += ['-c', field + '=' + json.dumps(value)]
            # With no explicit key on the official service, retain CLI login support.
        if model:
            argv += ['--model', model]
        prompt = '\n\n'.join(f"{m['role']}: {m['content']}" for m in self.history[-10:]
                            if m.get('role') in ('user', 'assistant')) + '\n\nUser: ' + self.prompt
        details = ('Authorize this entire CLI run in ' + str(self.workspace.root) + '.\n'
                   'It may read and edit files and run commands using the installed CLI permissions. '
                   'Forge cannot approve each internal CLI action. CLI configuration and MCP connections '
                   'are managed separately on the runner. Use a dedicated development account/container.\n\n'
                   + 'Command: ' + json.dumps(argv) + '\n\nTask: ' + self.prompt)
        if not self.approve('Run ' + exe + ' with coding tools', details):
            self.add('assistant', 'CLI run was not authorized.')
            return
        self.add('tool', 'Starting ' + exe + '. Output appears when this run finishes (up to 5 minutes).')
        result = command(argv, self.workspace.root, self.cancelled, 300, env=env, stdin=prompt)
        if not result.startswith('exit=0\n'):
            self.add('tool', result)
            raise ValueError('CLI did not finish successfully. Review its output above.')
        self.add('tool', result, tool=exe)
        raw = result.split('\n', 1)[1]
        summaries = []
        for line in raw.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get('is_error') or item.get('type') in ('error', 'turn.failed'):
                raise ValueError('CLI reported an error. Review its output above.')
            if isinstance(item.get('result'), str):
                summaries.append(item['result'])
            if item.get('type') == 'item.completed' and item.get('item', {}).get('type') == 'agent_message':
                summaries.append(item['item'].get('text', ''))
        self.add('assistant', '\n\n'.join(summaries) if summaries else raw)

    def work(self):
        try:
            if self.profile.get('kind') in ('claude-code', 'codex-cli'):
                self.cli()
            else:
                provider = Provider(self.profile, self.history + [{'role': 'user', 'content': self.prompt}],
                                    system_extra=self.system_extra)
                definitions, remote = copy.deepcopy(TOOLS), {}
                for ci, connector in enumerate(self.connectors):
                    for ti, t in enumerate(connector.tools):
                        name = f'mcp_{ci}_{ti}'
                        remote[name] = (connector, t['name'])
                        definitions.append({'name': name, 'description': f"{connector.name}: {t.get('description', t['name'])}"[:2000],
                                            'parameters': t.get('inputSchema', {'type': 'object', 'properties': {}})})
                for step in range(12):
                    if self.cancelled.is_set():
                        break
                    self.add('status', f'Agent step {step + 1}')
                    text, calls = provider.turn(definitions)
                    if self.cancelled.is_set():
                        break
                    if text:
                        self.add('assistant', text)
                    if not calls:
                        break
                    if len(calls) > 24:
                        raise ValueError('Provider requested too many tools in one step.')
                    results = []
                    for call in calls:
                        if self.cancelled.is_set():
                            break
                        name, args = call['name'], call['arguments']
                        try:
                            if not isinstance(args, dict):
                                raise ValueError('Tool arguments must be an object.')
                            diff = None
                            if name == 'write_file':
                                diff = self.workspace.diff(args['path'], args['content'])
                                if not self.approve('Edit ' + args['path'], diff or 'No text changes.'):
                                    raise ValueError('User denied this edit. Do not retry without a new user request.')
                            elif name == 'run_command' or name == 'run_python' or name == 'fetch_repo' or name in remote:
                                label = {'run_command': 'Run command', 'run_python': 'Run Python on this device',
                                         'fetch_repo': 'Fetch GitHub repo'}.get(name) or 'Connector: ' + remote[name][0].name + ' / ' + remote[name][1]
                                if not self.approve(label, json.dumps(args, indent=2)):
                                    raise ValueError('User denied this action. Do not retry without a new user request.')
                            if self.cancelled.is_set():
                                break
                            if diff is not None and self.workspace.diff(args['path'], args['content']) != diff:
                                raise ValueError('File changed after approval; request a fresh edit.')
                            if name in remote:
                                c, tool = remote[name]
                                result = json.dumps(c.call(tool, args))
                            else:
                                result = self.workspace.execute(name, args, self.cancelled)
                        except (ValueError, OSError, KeyError, TypeError) as exc:
                            result = 'Tool error: ' + str(exc)
                        if len(result) > 30000:
                            result = result[:30000] + '\n[truncated]'
                        self.add('tool', result, tool=name)
                        results.append((call, result))
                    provider.results(results)
                else:
                    self.add('assistant', 'Stopped at the 12-step limit. Review the changes and send a follow-up task.')
            self.status = 'cancelled' if self.cancelled.is_set() else 'done'
        except Exception as exc:
            self.status = 'cancelled' if self.cancelled.is_set() else 'error'
            # Never expose raw upstream errors or tracebacks to the phone.
            message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else f'{type(exc).__name__}: request failed. Check configuration and provider protocol.'
            self.add('error', message)
        finally:
            self.profile.pop('apiKey', None)
