import difflib
import io
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

SKIP = {'.git', '.env', '.ssh', '.aws', '.gradle', 'node_modules', '__pycache__', '.venv', 'build', '.forge'}
MAX_FILE = 300_000


def schema(name, description, props, required):
    return {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}}


STR = {'type': 'string'}
TOOLS = [
    schema('list_files', 'List project files in a relative directory.', {'path': STR}, ['path']),
    schema('read_file', 'Read a UTF-8 project file (up to 300 KB).', {'path': STR}, ['path']),
    schema('search_files', 'Literal text search in project files.', {'query': STR}, ['query']),
    schema('write_file', 'Create or replace a file. A diff is shown for user approval.', {'path': STR, 'content': STR}, ['path', 'content']),
    schema('run_command', 'Run a command as an argv array in the project, after user approval. No implicit shell.',
           {'argv': {'type': 'array', 'items': STR, 'minItems': 1}, 'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 120}}, ['argv']),
    schema('run_python', 'Execute a Python 3 snippet on the runner itself (same device that hosts the runner), after user approval. Stdout and stderr are returned.',
           {'code': STR, 'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 120}}, ['code']),
    schema('fetch_repo', 'Fetch a GitHub repository archive into the project, after user approval. Public repos need no token; private repos use the runner GITHUB_TOKEN.',
           {'repo': STR, 'ref': STR, 'dest': STR}, ['repo']),
    schema('github_read_file', 'Read a UTF-8 file from a GitHub repo. Needs GitHub connected in Cloud.',
           {'repo': STR, 'path': STR, 'ref': STR}, ['repo', 'path']),
    schema('github_create_pr', 'Open a GitHub pull request that creates or updates files, after user approval. Needs GitHub connected in Cloud.',
           {'repo': STR, 'title': STR, 'files': {'type': 'object'}, 'body': STR, 'base': STR}, ['repo', 'title', 'files']),
    schema('web_search', 'Search the web for current information, documentation, or solutions. Returns titles, URLs and snippets. No approval needed.',
           {'query': STR, 'count': {'type': 'integer', 'minimum': 1, 'maximum': 10}}, ['query']),
    schema('fetch_url', 'Fetch a public web page (http/https) and return its text content, up to ~8000 characters. For reading docs or articles. No approval needed.',
           {'url': STR}, ['url']),
]


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('Workspace must be a directory.')

    def path(self, value):
        if not isinstance(value, str) or Path(value).is_absolute():
            raise ValueError('A relative project path is required.')
        p = (self.root / value).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError('Path escapes the project.')
        if any(part in SKIP or part.startswith('.env') or part.endswith(('.pem', '.key', '.keystore'))
               for part in p.relative_to(self.root).parts):
            raise ValueError('This path is excluded from file tools.')
        return p

    def files(self, relative='.'):
        root = self.path(relative)
        result = []
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not Path(base, d).is_symlink())
            for name in sorted(files):
                p = Path(base, name)
                try:
                    self.path(str(p.relative_to(self.root)))
                except ValueError:
                    continue
                if not p.is_symlink():
                    result.append(str(p.relative_to(self.root)))
                if len(result) >= 2000:
                    return result
        return result

    def read(self, path):
        p = self.path(path)
        if p.stat().st_size > MAX_FILE:
            raise ValueError('File exceeds 300 KB.')
        return p.read_text(encoding='utf-8')

    def diff(self, path, content):
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise ValueError('Content must be UTF-8 text up to 300 KB.')
        p = self.path(path)
        before = self.read(path) if p.exists() else ''
        diff = ''.join(difflib.unified_diff(before.splitlines(True), content.splitlines(True),
                    fromfile=path + ' (before)', tofile=path + ' (after)'))
        if len(diff) > 100_000:
            raise ValueError('Edit is too large to review. Split it into smaller changes.')
        return diff

    def execute(self, name, args, cancelled):
        if name == 'list_files':
            return '\n'.join(self.files(args.get('path', '.')))
        if name == 'read_file':
            return self.read(args['path'])
        if name == 'search_files':
            q = args['query']
            if not isinstance(q, str) or not q:
                raise ValueError('Search query must be nonempty text.')
            found = []
            for f in self.files():
                if cancelled.is_set():
                    raise ValueError('Run cancelled.')
                try:
                    for n, line in enumerate(self.read(f).splitlines(), 1):
                        if q in line:
                            found.append(f'{f}:{n}: {line[:500]}')
                            if len(found) >= 100:
                                return '\n'.join(found)
                except (OSError, ValueError, UnicodeError):
                    continue
            return '\n'.join(found) or 'No matches.'
        if name == 'write_file':
            self.diff(args['path'], args['content'])
            p = self.path(args['path'])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args['content'], encoding='utf-8')
            return 'Saved ' + args['path']
        if name == 'run_command':
            return command(args['argv'], self.root, cancelled, args.get('timeout', 60))
        if name == 'run_python':
            return run_python(args['code'], self.root, cancelled, args.get('timeout', 60))
        if name == 'fetch_repo':
            return self.fetch_repo(args.get('repo', ''), args.get('ref', 'main') or 'main',
                                   args.get('dest', '') or None)
        if name == 'github_read_file':
            from . import github as _gh
            return _gh.read_file(args['repo'], args['path'], args.get('ref') or 'main', workspace=self)
        if name == 'github_create_pr':
            from . import github as _gh
            url = _gh.create_pr(args['repo'], args['title'], args.get('files') or {},
                                args.get('body') or '', args.get('base') or 'main', workspace=self)
            return 'Pull request opened: ' + url
        if name == 'web_search':
            from . import webtools as _wt
            return _wt.web_search(args['query'], args.get('count', 5))
        if name == 'fetch_url':
            from . import webtools as _wt
            return _wt.fetch_url(args['url'])
        raise ValueError('Unknown tool.')

    def fetch_repo(self, repo, ref='main', dest=None):
        import re as _re
        if not isinstance(repo, str) or not _re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo.strip()):
            raise ValueError('repo must look like owner/name.')
        repo = repo.strip()
        if not isinstance(ref, str) or not _re.fullmatch(r'[A-Za-z0-9_.\-/]{1,100}', ref.strip()):
            raise ValueError('ref must be a branch or tag name.')
        ref = ref.strip()
        owner, project = repo.split('/')
        target = self.path(dest or project)
        url = f'https://codeload.github.com/{owner}/{project}/zip/refs/heads/{ref}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Forge-Agent/0.4',
                                                   'Accept': 'application/zip'})
        token = os.environ.get('GITHUB_TOKEN', '').strip()
        if token:
            req.add_header('Authorization', 'Bearer ' + token)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                if response.status != 200:
                    raise ValueError(f'GitHub returned HTTP {response.status}. Check the repo name, ref, and token.')
                raw = response.read(500_000_001)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ValueError('Repository or ref not found. For a private repo, set GITHUB_TOKEN on the runner.') from None
            raise ValueError(f'GitHub returned HTTP {e.code}. Check the repo name, ref, and token.') from None
        except urllib.error.URLError:
            raise ValueError('Cannot reach GitHub. Check runner connectivity.') from None
        if len(raw) > 500_000_000:
            raise ValueError('Repository archive exceeds 500 MB.')
        target.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            members = z.infolist()
            if not members:
                raise ValueError('Repository archive is empty.')
            prefix = members[0].filename.split('/')[0] + '/'
            for item in members:
                name = item.filename
                if not name.startswith(prefix) or name == prefix:
                    continue
                rel = name[len(prefix):]
                destination = (target / rel).resolve()
                if not destination.is_relative_to(target):
                    raise ValueError('Archive contains an unsafe path; fetch aborted.')
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(item) as source, destination.open('wb') as out:
                        out.write(source.read())
                    count += 1
                if count > 100000:
                    raise ValueError('Repository has too many files to fetch.')
        return f'Fetched {repo}@{ref} into {target.relative_to(self.root)} ({count} files).'


def run_python(code, cwd, cancelled, timeout=60):
    """Execute a Python snippet in-process on the runner device; captures stdout/stderr."""
    if not isinstance(code, str) or not code.strip() or len(code) > 100_000:
        raise ValueError('code must be 1-100000 characters of Python source.')
    timeout = max(1, min(int(timeout), 120))
    import contextlib
    buffer, result, error = io.StringIO(), {}, []
    def target():
        namespace = {'__name__': '__forge__'}
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                exec(compile(code, '<forge>', 'exec'), namespace)
        except BaseException as exc:  # Never leak tracebacks with paths; report the error only.
            error.append(f'{type(exc).__name__}: {exc}')
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise ValueError('Python snippet exceeded its timeout. Keep snippets short or split the work.')
    output = buffer.getvalue()
    if len(output) > 100_000:
        output = output[:100_000] + '\n[truncated]'
    if error:
        return 'error\n' + error[0] + ('\n' + output if output else '')
    return ('ok\n' + output) if output else 'ok (no output)'


def child_env():
    # Do not pass runner/provider credentials to model-generated commands.
    allowed = ('PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'TERM', 'COMSPEC', 'PATHEXT')
    return {k: os.environ[k] for k in allowed if k in os.environ}


def command(argv, cwd, cancelled, timeout=60, env=None, stdin=None):
    if not isinstance(argv, list) or not argv or len(argv) > 128 or not all(isinstance(x, str) and '\0' not in x for x in argv):
        raise ValueError('argv must be a nonempty array of strings.')
    timeout = max(1, min(int(timeout), 300 if env is not None else 120))
    proc = subprocess.Popen(argv, cwd=cwd, env=env if env is not None else child_env(),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=(os.name != 'nt'))
    chunks, overflow = [], threading.Event()
    def drain():
        total = 0
        try:
            while True:
                data = proc.stdout.read(4096)
                if not data:
                    break
                total += len(data)
                if total <= 200_000:
                    chunks.append(data)
                else:
                    overflow.set()
        finally:
            proc.stdout.close()
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    if stdin is not None:
        try:
            proc.stdin.write(stdin.encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass
    deadline, reason = time.monotonic() + timeout, ''
    while proc.poll() is None:
        if cancelled.is_set() or time.monotonic() > deadline or overflow.is_set():
            reason = 'cancelled' if cancelled.is_set() else 'timeout or output limit'
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            break
        cancelled.wait(.1)
    proc.wait()
    reader.join(timeout=2)
    return f'exit={proc.returncode}' + (f' ({reason})' if reason else '') + '\n' + b''.join(chunks).decode('utf-8', errors='replace')
