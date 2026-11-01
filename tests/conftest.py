import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


class Site:
    """A little web server on loopback: pages by path, and a log of what it got."""

    def __init__(self, host="127.0.0.1"):
        self.pages = {}
        self.requests = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.requests.append((self.path, dict(self.headers)))
                path = urlsplit(self.path).path
                page = site.pages.get(path)
                if page is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if callable(page):
                    page = page(parse_qs(urlsplit(self.path).query))
                status, headers, body = page if isinstance(page, tuple) else (200, {}, page)
                data = body.encode()
                self.send_response(status)
                headers = {"Content-Type": "text/html; charset=utf-8", **headers}
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer((host, 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://{host}:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


@pytest.fixture
def private_site():
    """A second server, on an address the browser tests deny."""
    s = Site("127.0.0.2")
    yield s
    s.close()
