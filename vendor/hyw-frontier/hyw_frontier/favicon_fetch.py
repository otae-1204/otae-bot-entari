"""Credential-free favicon discovery/decoding in a parent-deadlined subprocess.

Only same-site /favicon.ico and icons declared by the public homepage are tried.
All network hops use media_fetch's public-IP pinning; no browser or SVG execution.
"""
from html.parser import HTMLParser
from pathlib import Path
import sys
from urllib.parse import urljoin, urlsplit

# -I deliberately excludes the script directory; add only this trusted source directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from favicon_assets import MAX_DOWNLOAD_BYTES, normalize_icon
from media_fetch import fetch


class IconLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'link' or len(self.links) >= 8:
            return
        attrs = dict(attrs)
        rel = (attrs.get('rel') or '').lower().split()
        href = attrs.get('href') or ''
        if href and any(value in rel for value in ('icon', 'apple-touch-icon', 'apple-touch-icon-precomposed')):
            self.links.append(href)


def download_icon(url):
    raw = fetch(url, max_bytes=MAX_DOWNLOAD_BYTES, accept='image/*')
    return normalize_icon(raw)


def resolve(origin):
    parts = urlsplit(origin)
    if parts.path or parts.query or parts.fragment or len(origin) > 512:
        raise ValueError('invalid_origin')
    tried = {origin + '/favicon.ico'}
    try:
        return download_icon(origin + '/favicon.ico')
    except Exception:
        pass
    try:
        raw, page_url = fetch(origin + '/', max_bytes=512 * 1024,
                              accept='text/html', include_url=True)
        parser = IconLinks()
        parser.feed(raw.decode('utf-8', errors='replace'))
        links = [urljoin(page_url, href) for href in parser.links]
    except Exception:
        links = []
    links.append(origin + '/apple-touch-icon.png')
    for url in links:
        if url in tried or len(url) > 2048 or urlsplit(url).path.lower().endswith('.svg'):
            continue
        if len(tried) >= 4:
            break
        tried.add(url)
        try:
            return download_icon(url)
        except Exception:
            pass
    raise ValueError('icon_unavailable')


if __name__ == '__main__':
    try:
        origin = sys.stdin.buffer.read(513).decode('utf-8')
        sys.stdout.buffer.write(resolve(origin))
    except Exception:
        # No raw URLs, exceptions, credentials or website responses in the parent trace.
        sys.exit(2)
