"""GitHub integration: read repo files and open pull requests via the REST API.

The token comes from the GITHUB_TOKEN environment variable, or — on a shared
backend — from the device's own `github_token` file in its workspace directory.
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API = 'https://api.github.com'


def token(workspace=None, silent=False):
    t = os.environ.get('GITHUB_TOKEN', '').strip()
    if not t and workspace is not None:
        try:
            t = Path(workspace.root, 'github_token').read_text(encoding='utf-8').strip()
        except OSError:
            t = ''
    if not t and not silent:
        raise ValueError('GitHub is not connected. Add a token in Cloud → GitHub, or set GITHUB_TOKEN on the runner.')
    return t


def save_token(workspace, tok, device):
    # Single-user mode keeps the previous env behavior; on a shared backend the
    # token is stored in the device's own workspace directory.
    if device is None and os.environ.get('FORGE_SHARED') != '1':
        os.environ['GITHUB_TOKEN'] = tok
    else:
        Path(workspace.root, 'github_token').write_text(tok, encoding='utf-8')


def clear_token(workspace, device):
    if device is None and os.environ.get('FORGE_SHARED') != '1':
        os.environ.pop('GITHUB_TOKEN', None)
    else:
        try:
            Path(workspace.root, 'github_token').unlink()
        except OSError:
            pass


def _request(method, path, body=None, auth=None):
    tok = (auth or '').strip() or token()
    req = urllib.request.Request(API + path, method=method, headers={
        'Authorization': 'Bearer ' + tok,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'Forge-Agent/0.5',
        'X-GitHub-Api-Version': '2022-11-28'})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get('message', '')
        except Exception:
            detail = ''
        if e.code == 401:
            raise ValueError('GitHub rejected the token (HTTP 401). Check it in Cloud → GitHub.')
        if e.code == 404:
            raise ValueError('Not found on GitHub (HTTP 404). Check the owner, repo, and token permissions.')
        if e.code == 403:
            raise ValueError('GitHub refused (HTTP 403). ' + detail)
        raise ValueError(('GitHub API error (HTTP %d). %s' % (e.code, detail)).strip())


def check_repo(value):
    m = re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', (value or '').strip())
    if not m or '..' in m.group(0).split('/'):
        raise ValueError('repo must look like owner/name.')
    return m.group(0)


def check_path(value):
    if not isinstance(value, str) or not value or value.startswith('/') or '..' in value.split('/'):
        raise ValueError('path must be a relative file path.')
    return value


def me(workspace=None, auth=None):
    """Return the token owner's login, or raise."""
    login = _request('GET', '/user', auth=auth or token(workspace)).get('login', '')
    if not login:
        raise ValueError('GitHub did not return a user for this token.')
    return login


def read_file(repo, path, ref='main', workspace=None):
    repo = check_repo(repo)
    path = check_path(path)
    r = _request('GET', '/repos/%s/contents/%s?ref=%s' % (repo, path, ref or 'main'),
                 auth=token(workspace))
    if r.get('type') != 'file' or 'content' not in r:
        raise ValueError('That path is not a file in %s.' % repo)
    return base64.b64decode(r['content']).decode('utf-8', 'replace')


def create_pr(repo, title, files, body='', base='main', branch=None, workspace=None):
    """Open a pull request that creates/updates files. files: {path: content}."""
    repo = check_repo(repo)
    if not isinstance(title, str) or not title.strip():
        raise ValueError('PR title is required.')
    if not isinstance(files, dict) or not files:
        raise ValueError('files must be a non-empty object of path → content.')
    for p, content in files.items():
        check_path(p)
        if not isinstance(content, str):
            raise ValueError('Content for %s must be text.' % p)
    auth = token(workspace)
    base = (base or 'main').strip()
    base_sha = _request('GET', '/repos/%s/git/ref/heads/%s' % (repo, base), auth=auth)['object']['sha']
    blobs = []
    for p, content in files.items():
        b = _request('POST', '/repos/%s/git/blobs' % repo,
                     {'content': base64.b64encode(content.encode()).decode(), 'encoding': 'base64'}, auth=auth)
        blobs.append({'path': p, 'mode': '100644', 'type': 'blob', 'sha': b['sha']})
    tree = _request('POST', '/repos/%s/git/trees' % repo, {'base_tree': base_sha, 'tree': blobs}, auth=auth)
    commit = _request('POST', '/repos/%s/git/commits' % repo,
                      {'message': title.strip(), 'tree': tree['sha'], 'parents': [base_sha]}, auth=auth)
    branch = (branch or 'forge/%d' % int(time.time())).strip()
    _request('POST', '/repos/%s/git/refs' % repo, {'ref': 'refs/heads/' + branch, 'sha': commit['sha']}, auth=auth)
    pr = _request('POST', '/repos/%s/pulls' % repo,
                  {'title': title.strip(), 'head': branch, 'base': base, 'body': body or ''}, auth=auth)
    url = pr.get('html_url', '')
    if not url:
        raise ValueError('GitHub did not return a pull request URL.')
    return url
