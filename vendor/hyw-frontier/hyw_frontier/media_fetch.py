"""Isolated, credential-free image fetcher. Media parent enforces a 2.5-second lifetime.

Run by file path with Python -I, so package/model imports do not slow down downloads.
DNS and every redirect are validated, and the connection is pinned to a public IP.
"""
import http.client
import ipaddress
import socket
import ssl
import sys
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT = 2.5
# Clash/mihomo fake-ip uses the RFC 2544 benchmarking range. It is not a LAN address.
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def _routable(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    if address.is_global:
        return True
    return address.version == 4 and address in _FAKE_IP


class HTTPStatusError(ValueError):
    def __init__(self, status: int):
        super().__init__('http_error')
        self.status = status


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def fetch(url, *, max_bytes=MAX_BYTES, timeout=DEFAULT_TIMEOUT,
          accept='image/jpeg,image/png,image/webp,image/gif', include_url=False):
    for redirect in range(3):
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 80, 443)
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError('invalid_url')
        host = parts.hostname.encode('idna').decode('ascii')
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not _routable(row[4][0]) for row in addresses):
            raise ValueError('private_address')
        address = addresses[0][4][0]
        connection = (PinnedHTTPS(host, port, address, timeout) if parts.scheme == 'https'
                      else http.client.HTTPConnection(address, port, timeout=timeout))
        try:
            target = (parts.path or '/') + ('?' + parts.query if parts.query else '')
            connection.request('GET', target, headers={
                'Host': parts.netloc, 'Accept': accept,
                'Accept-Encoding': 'identity', 'User-Agent': 'Hyw-Frontier/0.1 image-preview',
            })
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or redirect == 2:
                    raise ValueError('redirect_limit')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise HTTPStatusError(response.status)
            length = response.getheader('Content-Length')
            if length and int(length) > max_bytes:
                raise ValueError('too_large')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise ValueError('encoded_response')
            raw = response.read(max_bytes + 1)
            if not raw or len(raw) > max_bytes:
                raise ValueError('too_large')
            return (raw, url) if include_url else raw
        finally:
            connection.close()
    raise ValueError('redirect_limit')


if __name__ == '__main__':
    try:
        timeout = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TIMEOUT
        url = sys.stdin.buffer.read(8193).decode('utf-8')
        if len(url) > 8192:
            raise ValueError('invalid_url')
        sys.stdout.buffer.write(fetch(url, timeout=timeout))
    except HTTPStatusError as error:
        # Only a numeric status crosses the worker boundary, never response bodies or headers.
        sys.stderr.write(f'http_{error.status}')
        sys.exit(2)
    except Exception:
        # No raw URLs, headers or network diagnostics cross the worker boundary.
        sys.exit(2)
