import io
import unittest
import urllib.error
from unittest.mock import patch
from runner import webtools
from runner.workspace import Workspace, TOOLS
import tempfile


DDG_HTML = (
    '<html><body><table>'
    '<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&amp;rut=abc" class=\'result-link\'>Example Docs</a></td></tr>'
    '<tr><td class=\'result-snippet\'>Official documentation for the example library.</td></tr>'
    '<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=http%3A%2F%2Fblog.example.org%2Fpost&amp;rut=def" class=\'result-link\'>A blog post</a></td></tr>'
    '<tr><td class=\'result-snippet\'>Someone wrote about it.</td></tr>'
    '</table></body></html>'
)

PAGE_HTML = (
    '<html><head><title>T</title><style>.x{color:red}</style></head>'
    '<body><nav>menu</nav><h1>Hello</h1><script>alert(1)</script>'
    '<p>World of <b>text</b>.</p></body></html>'
)


class FakeResponse:
    def __init__(self, body, ctype='text/html'):
        self._body, self._ctype = body, ctype
        self.headers = self

    def get_content_type(self):
        return self._ctype

    def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class WebToolsTests(unittest.TestCase):
    def test_search_parses_results(self):
        with patch.object(webtools.urllib.request, 'urlopen', return_value=FakeResponse(DDG_HTML.encode())):
            out = webtools.web_search('example library', 5)
        self.assertIn('Example Docs', out)
        self.assertIn('https://example.com/docs', out)
        self.assertIn('Official documentation', out)
        self.assertIn('http://blog.example.org/post', out)

    def test_search_count_limit(self):
        with patch.object(webtools.urllib.request, 'urlopen', return_value=FakeResponse(DDG_HTML.encode())):
            out = webtools.web_search('x', 1)
        self.assertIn('Example Docs', out)
        self.assertNotIn('blog post', out)

    def test_search_empty_query(self):
        with self.assertRaises(ValueError):
            webtools.web_search('   ')

    def test_search_unreachable(self):
        with patch.object(webtools.urllib.request, 'urlopen', side_effect=OSError('down')):
            with self.assertRaises(ValueError) as e:
                webtools.web_search('x')
        self.assertIn('unreachable', str(e.exception))

    def test_search_no_results(self):
        with patch.object(webtools.urllib.request, 'urlopen', return_value=FakeResponse(b'<html></html>')):
            self.assertEqual(webtools.web_search('x'), 'No results found.')

    def test_fetch_extracts_text(self):
        with patch.object(webtools.urllib.request, 'urlopen', return_value=FakeResponse(PAGE_HTML.encode())), \
             patch.object(webtools, '_is_public_url', return_value=True):
            out = webtools.fetch_url('https://example.com/page')
        self.assertIn('Hello', out)
        self.assertIn('World of text.', out)
        self.assertNotIn('alert(1)', out)
        self.assertNotIn('menu', out)

    def test_fetch_rejects_private(self):
        with self.assertRaises(ValueError):
            webtools.fetch_url('http://127.0.0.1/admin')
        with self.assertRaises(ValueError):
            webtools.fetch_url('http://169.254.169.254/latest')

    def test_fetch_rejects_non_text(self):
        with patch.object(webtools.urllib.request, 'urlopen',
                          return_value=FakeResponse(b'\x89PNG', 'image/png')), \
             patch.object(webtools, '_is_public_url', return_value=True):
            with self.assertRaises(ValueError) as e:
                webtools.fetch_url('https://example.com/x.png')
        self.assertIn('Not a readable page', str(e.exception))

    def test_fetch_http_error(self):
        err = urllib.error.HTTPError('https://example.com/404', 404, 'NF', {}, io.BytesIO())
        with patch.object(webtools.urllib.request, 'urlopen', side_effect=err), \
             patch.object(webtools, '_is_public_url', return_value=True):
            with self.assertRaises(ValueError) as e:
                webtools.fetch_url('https://example.com/404')
        self.assertIn('404', str(e.exception))

    def test_tools_registered(self):
        names = {t['name'] for t in TOOLS}
        self.assertIn('web_search', names)
        self.assertIn('fetch_url', names)

    def test_execute_dispatch(self):
        temp = tempfile.TemporaryDirectory()
        try:
            ws = Workspace(temp.name)
            with patch.object(webtools, 'web_search', return_value='r1') as m:
                self.assertEqual(ws.execute('web_search', {'query': 'q', 'count': 3}, None), 'r1')
                m.assert_called_once_with('q', 3)
            with patch.object(webtools, 'fetch_url', return_value='page') as m:
                self.assertEqual(ws.execute('fetch_url', {'url': 'https://example.com'}, None), 'page')
                m.assert_called_once_with('https://example.com')
        finally:
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
