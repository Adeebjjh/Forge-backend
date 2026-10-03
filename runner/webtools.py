"""Web research tools for the agent: web search + page fetch. Stdlib only.

Both are read-only, so the agent can use them without asking first.
"""
import html.parser
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

UA = 'Forge-Agent/0.5'


class _TextExtract(html.parser.HTMLParser):
    """Crude HTML -> readable text: drops scripts/styles/nav, collapses whitespace."""
    SKIP = {'script', 'style', 'noscript', 'nav', 'footer', 'header', 'aside', 'form'}
    BREAK = {'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'article', 'section', 'br', 'blockquote'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BREAK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BREAK:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        lines = [re.sub(r'\s+', ' ', ln).strip() for ln in ''.join(self.parts).split('\n')]
        return '\n'.join(ln for ln in lines if ln)


class _DDGResults(html.parser.HTMLParser):
    """Parse DuckDuckGo lite results: <a class=result-link> + <td class=result-snippet>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self._link, self._in_snippet, self._snippet = [], None, False, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a' and a.get('class') == 'result-link':
            href = a.get('href', '')
            m = re.search(r'[?&]uddg=([^&]+)', href)
            url = urllib.parse.unquote(m.group(1)) if m else href
            if url.startswith('//'):
                url = 'https:' + url
            self._link = {'title': '', 'url': url, 'snippet': ''}
        elif tag == 'td' and a.get('class') == 'result-snippet':
            self._in_snippet, self._snippet = True, []

    def handle_data(self, data):
        if self._in_snippet:
            self._snippet.append(data)
        elif self._link is not None:
            self._link['title'] += data

    def handle_endtag(self, tag):
        if tag == 'td' and self._in_snippet:
            self._in_snippet = False
            if self._link is not None:
                self._link['snippet'] = ' '.join(''.join(self._snippet).split())
                if self._link['title'].strip() and self._link['url'].startswith('http'):
                    self._link['title'] = self._link['title'].strip()
                    self.results.append(self._link)
                self._link = None


def _is_public_url(url):
    """Only public http(s) hosts — blocks SSRF against the runner's own network."""
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        return True
    except Exception:
        return False


def web_search(query, count=5):
    if not isinstance(query, str) or not query.strip():
        raise ValueError('Search query must be nonempty text.')
    try:
        count = max(1, min(int(count or 5), 10))
    except (TypeError, ValueError):
        count = 5
    url = 'https://lite.duckduckgo.com/lite/?q=' + urllib.parse.quote_plus(query.strip())
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read(500_000).decode('utf-8', 'replace')
    except Exception:
        raise ValueError('Web search is unreachable right now. Try again later.') from None
    parser = _DDGResults()
    try:
        parser.feed(html)
    except Exception:
        raise ValueError('Could not parse search results. Try again later.') from None
    results = parser.results[:count]
    if not results:
        return 'No results found.'
    return '\n\n'.join(f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results))


def fetch_url(url):
    if not isinstance(url, str) or not _is_public_url(url.strip()):
        raise ValueError('URL must be a public http(s) address.')
    url = url.strip()
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,text/plain'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ctype = (r.headers.get_content_type() or '').lower()
            if not (ctype.startswith('text/') or ctype == 'application/xhtml+xml'):
                raise ValueError(f'Not a readable page (content type: {ctype or "unknown"}).')
            raw = r.read(1_000_000)
    except urllib.error.HTTPError as e:
        raise ValueError(f'Page returned HTTP {e.code}.') from None
    except ValueError:
        raise
    except Exception:
        raise ValueError('Could not fetch that page. Check the URL and try again.') from None
    text = raw.decode('utf-8', 'replace')
    if 'html' in ctype:
        parser = _TextExtract()
        try:
            parser.feed(text)
            text = parser.text()
        except Exception:
            pass
    text = text.strip()
    if not text:
        return 'Page had no readable text.'
    return text[:8000]
