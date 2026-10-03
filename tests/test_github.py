"""GitHub integration: repo validation, file reads, PR creation, and builtin skills."""
import base64
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import github
from runner import skills as skill_module
from runner.workspace import Workspace, TOOLS


class GitHubValidationTests(unittest.TestCase):
    def test_check_repo(self):
        self.assertEqual(github.check_repo('octo/repo'), 'octo/repo')
        for bad in ('', 'nope', 'a/b/c', '../x', 'a b/c'):
            with self.assertRaises(ValueError):
                github.check_repo(bad)

    def test_check_path(self):
        self.assertEqual(github.check_path('src/a.py'), 'src/a.py')
        for bad in ('', '/abs', '../up', 'a/../../b'):
            with self.assertRaises(ValueError):
                github.check_path(bad)

    def test_token_required(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': ''}):
            with self.assertRaisesRegex(ValueError, 'not connected'):
                github.token()


class GitHubApiTests(unittest.TestCase):
    def test_read_file(self):
        payload = base64.b64encode('hello'.encode()).decode()
        with patch('runner.github._request', return_value={'type': 'file', 'content': payload}) as m:
            with patch.dict(os.environ, {'GITHUB_TOKEN': 'x' * 40}):
                self.assertEqual(github.read_file('o/r', 'f.txt', 'dev'), 'hello')
        method, path = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(method, 'GET')
        self.assertIn('/repos/o/r/contents/f.txt', path)
        self.assertIn('ref=dev', path)

    def test_read_file_rejects_directory(self):
        with patch('runner.github._request', return_value={'type': 'dir'}):
            with patch.dict(os.environ, {'GITHUB_TOKEN': 'x' * 40}):
                with self.assertRaises(ValueError):
                    github.read_file('o/r', 'somedir')

    def test_create_pr_sequence(self):
        calls = []

        def fake(method, path, body=None, **kw):
            calls.append((method, path))
            if path.endswith('/git/ref/heads/main'):
                return {'object': {'sha': 'base'}}
            if '/git/blobs' in path:
                return {'sha': 'blob1'}
            if '/git/trees' in path:
                return {'sha': 'tree1'}
            if '/git/commits' in path:
                return {'sha': 'commit1'}
            if '/git/refs' in path:
                return {}
            if path.endswith('/pulls'):
                self.assertEqual(body['title'], 'Add thing')
                self.assertEqual(body['base'], 'main')
                return {'html_url': 'https://github.com/o/r/pull/7'}
            raise AssertionError(path)

        with patch('runner.github._request', side_effect=fake):
            with patch.dict(os.environ, {'GITHUB_TOKEN': 'x' * 40}):
                url = github.create_pr('o/r', 'Add thing', {'a.txt': 'hi'}, body='does stuff')
        self.assertEqual(url, 'https://github.com/o/r/pull/7')
        methods = [c[0] for c in calls]
        self.assertIn('POST', methods)
        self.assertTrue(any(p.endswith('/pulls') for _, p in calls))

    def test_create_pr_validates(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'x' * 40}):
            with self.assertRaises(ValueError):
                github.create_pr('o/r', '', {'a.txt': 'x'})
            with self.assertRaises(ValueError):
                github.create_pr('o/r', 't', {})


class GitHubToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Workspace(self.temp.name)

    def test_tools_registered(self):
        names = [t['name'] for t in TOOLS]
        self.assertIn('github_read_file', names)
        self.assertIn('github_create_pr', names)

    def test_read_dispatch(self):
        with patch('runner.github.read_file', return_value='data') as m:
            out = self.ws.execute('github_read_file', {'repo': 'o/r', 'path': 'f.txt'}, threading.Event())
        self.assertEqual(out, 'data')
        m.assert_called_once_with('o/r', 'f.txt', 'main', workspace=self.ws)

    def test_pr_dispatch(self):
        with patch('runner.github.create_pr', return_value='https://github.com/o/r/pull/1') as m:
            out = self.ws.execute('github_create_pr',
                                  {'repo': 'o/r', 'title': 'T', 'files': {'a.txt': 'x'}},
                                  threading.Event())
        self.assertIn('https://github.com/o/r/pull/1', out)
        m.assert_called_once_with('o/r', 'T', {'a.txt': 'x'}, '', 'main', workspace=self.ws)


class BuiltinSkillsTests(unittest.TestCase):
    def test_builtins_listed(self):
        names = [s['name'] for s in skill_module.list_skills()]
        for expected in ('Code Review', 'Write Tests', 'Explain Code', 'Fix Bug', 'Open PR'):
            self.assertIn(expected, names)

    def test_builtin_lookup(self):
        s = skill_module.get_skill('Code Review')
        self.assertIsNotNone(s)
        self.assertTrue(s['instructions'])

    def test_builtin_tools_exist(self):
        known = [t['name'] for t in TOOLS]
        for s in skill_module.list_skills():
            for t in s.get('tools', []):
                self.assertIn(t, known, 'skill %s references unknown tool %s' % (s['name'], t))

    def test_user_skill_overrides_builtin_name(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {'FORGE_SKILLS_DIR': d}):
                skill_module.save_skill({'name': 'Mine', 'description': 'd', 'instructions': 'i', 'tools': []}, [])
                names = [s['name'] for s in skill_module.list_skills()]
                self.assertIn('Mine', names)
                self.assertIn('Code Review', names)
