"""The browser's egress: a small forward proxy on loopback that the browser
sends everything through.

Every request (each navigation, each redirect hop, each subresource) arrives
here before any connection is made, so the deny networks are checked on all of
them, and the connection goes to the very address that was checked (no DNS
rebinding). From here it goes direct, or through the upstream proxy
(HTTPS_PROXY and friends, with basic auth), unless NO_PROXY says otherwise.

HTTPS is a CONNECT tunnel: the TLS handshake is the browser's own, end to end,
so the sites see Firefox's fingerprint, not ours.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import logging
import os
import ssl
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import ProxyConfig
from .guard import Guard, Refused

log = logging.getLogger(__name__)

REFUSED_HEADER = "X-Browser-Mcp-Refused"
_MAX_HEAD = 64 * 1024


@dataclass
class Upstream:
    host: str
    port: int
    tls: bool
    auth: str | None  # the Proxy-Authorization value


class NoProxy:
    """NO_PROXY as curl reads it: names (a leading dot or *. optional, so
    example.com covers its subdomains), IPs and CIDRs, host:port, or *."""

    def __init__(self, spec: str):
        self.all = False
        self.names: list[tuple[str, int | None]] = []
        self.nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for item in (s.strip().lower() for s in spec.replace(";", ",").split(",")):
            if not item:
                continue
            if item == "*":
                self.all = True
                continue
            try:
                self.nets.append(ipaddress.ip_network(item.strip("[]"), strict=False))
                continue
            except ValueError:
                pass
            port = None
            if item.count(":") == 1:
                item, p = item.split(":")
                port = int(p) if p.isdigit() else None
            self.names.append((item.removeprefix("*").lstrip("."), port))

    def bypass(self, host: str, port: int) -> bool:
        if self.all:
            return True
        host = host.lower().strip("[]").rstrip(".")
        try:
            a = ipaddress.ip_address(host)
            if any(a in n for n in self.nets if n.version == a.version):
                return True
        except ValueError:
            pass
        for name, p in self.names:
            if p is not None and p != port:
                continue
            if host == name or host.endswith("." + name):
                return True
        return False


def upstream_from(cfg: ProxyConfig, env: dict[str, str] | None = None) -> tuple[Upstream | None, Upstream | None, NoProxy]:
    """The upstream proxies for https and http, and the bypass list."""
    env = os.environ if env is None else env

    def getenv(name: str) -> str:
        return env.get(name) or env.get(name.lower()) or ""

    https_url = cfg.url or getenv("HTTPS_PROXY") or getenv("ALL_PROXY")
    http_url = cfg.url or getenv("HTTP_PROXY") or getenv("ALL_PROXY")
    no_proxy = NoProxy(cfg.no_proxy or getenv("NO_PROXY"))
    password = None
    if cfg.password_file:
        password = Path(cfg.password_file).read_text().strip()
    elif env.get("BROWSER_MCP_PROXY_PASSWORD"):
        password = env["BROWSER_MCP_PROXY_PASSWORD"]

    def parse(url: str) -> Upstream | None:
        if not url:
            return None
        if "://" not in url:
            url = "http://" + url
        p = urlsplit(url)
        if p.scheme not in ("http", "https"):
            raise ValueError(f"proxy {p.scheme}://: only http:// and https:// proxies are supported")
        user = cfg.username or (unquote(p.username) if p.username else "")
        pw = password if password is not None else (unquote(p.password) if p.password else "")
        auth = None
        if user:
            auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        return Upstream(p.hostname, p.port or (443 if p.scheme == "https" else 80), p.scheme == "https", auth)

    return parse(https_url), parse(http_url), no_proxy


class EgressProxy:
    def __init__(self, guard: Guard, https: Upstream | None, http: Upstream | None,
                 no_proxy: NoProxy, ssl_context: ssl.SSLContext | None = None,
                 connect_timeout: float = 20.0):
        self.guard = guard
        self.https = https
        self.http = http
        self.no_proxy = no_proxy
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.connect_timeout = connect_timeout
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        # Recent refusals, so a tool can say why a navigation failed.
        self.refusals: deque[tuple[float, str, str]] = deque(maxlen=200)
        # Hosts that went through the upstream proxy (for tests and logs).
        self.via_upstream: deque[str] = deque(maxlen=200)

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None

    def refusals_since(self, t: float) -> list[tuple[str, str]]:
        return [(h, r) for (at, h, r) in self.refusals if at >= t]

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
            writer.close()
            return
        try:
            line, *header_lines = head.decode("latin-1").split("\r\n")
            method, target, version = line.split(" ", 2)
            if method == "CONNECT":
                await self._connect(target, version, reader, writer)
            else:
                await self._forward(method, target, version, header_lines, reader, writer)
        except Refused as e:
            host = target.rsplit(":", 1)[0] if method == "CONNECT" else (urlsplit(target).hostname or "")
            self.refusals.append((time.monotonic(), host, str(e)))
            log.info("egress refused %s: %s", host, e)
            await _respond(writer, 403, "Forbidden", str(e), {REFUSED_HEADER: str(e)})
        except (ValueError, IndexError):
            await _respond(writer, 400, "Bad Request", "bad request")
        except (OSError, TimeoutError) as e:
            await _respond(writer, 502, "Bad Gateway", f"can't connect: {e}")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def upstream_for(self, url: str) -> Upstream | None:
        """The upstream proxy a URL would go through, if any."""
        p = urlsplit(url)
        https = p.scheme == "https"
        return self._route(p.hostname or "", p.port or (443 if https else 80), self.https if https else self.http)

    def _route(self, host: str, port: int, upstream: Upstream | None) -> Upstream | None:
        if upstream is None or self.no_proxy.bypass(host, port):
            return None
        return upstream

    async def _open(self, host: str, port: int, upstream: Upstream | None):
        """A connection to the checked address, or to the upstream proxy."""
        addrs = await self.guard.check_host(host, port, via_proxy=upstream is not None)
        if upstream is not None:
            self.via_upstream.append(host)
            return await asyncio.wait_for(asyncio.open_connection(
                upstream.host, upstream.port,
                ssl=self.ssl_context if upstream.tls else None,
                server_hostname=upstream.host if upstream.tls else None), self.connect_timeout)
        last: Exception | None = None
        for a in addrs:
            try:
                return await asyncio.wait_for(asyncio.open_connection(str(a), port), self.connect_timeout)
            except (OSError, TimeoutError) as e:
                last = e
        raise last or OSError(f"no address for {host}")

    async def _connect(self, target: str, version: str, reader, writer) -> None:
        host, _, port_s = target.rpartition(":")
        host, port = host.strip("[]"), int(port_s)
        upstream = self._route(host, port, self.https)
        r, w = await self._open(host, port, upstream)
        try:
            if upstream is not None:
                req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                if upstream.auth:
                    req += f"Proxy-Authorization: {upstream.auth}\r\n"
                w.write((req + "\r\n").encode("latin-1"))
                await w.drain()
                resp = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), self.connect_timeout)
                status = resp.split(b" ", 2)[1]
                if status != b"200":
                    # The upstream's answer (a 407, a 403 of its own) goes back as is.
                    writer.write(resp)
                    with contextlib.suppress(Exception):
                        writer.write(await asyncio.wait_for(r.read(_MAX_HEAD), 2))
                    await writer.drain()
                    return
            writer.write(f"{version} 200 Connection established\r\n\r\n".encode("latin-1"))
            await writer.drain()
            await _pipe(reader, writer, r, w)
        finally:
            with contextlib.suppress(Exception):
                w.close()

    async def _forward(self, method: str, target: str, version: str, header_lines: list[str], reader, writer) -> None:
        p = urlsplit(target)
        if p.scheme != "http" or not p.hostname:
            raise ValueError("not a proxy request")
        port = p.port or 80
        upstream = self._route(p.hostname, port, self.http)
        headers = [h for h in header_lines if h and not h.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
        headers.append("Connection: close")
        if upstream is not None:
            first = f"{method} {target} {version}"
            if upstream.auth:
                headers.append(f"Proxy-Authorization: {upstream.auth}")
        else:
            path = p.path or "/"
            first = f"{method} {path}{'?' + p.query if p.query else ''} {version}"
        r, w = await self._open(p.hostname, port, upstream)
        try:
            w.write(("\r\n".join([first, *headers]) + "\r\n\r\n").encode("latin-1"))
            await w.drain()
            await _pipe(reader, writer, r, w)
        finally:
            with contextlib.suppress(Exception):
                w.close()


async def _respond(writer, code: int, reason: str, body: str, headers: dict[str, str] | None = None) -> None:
    data = body.encode()
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    with contextlib.suppress(Exception):
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n{extra}Connection: close\r\n\r\n".encode() + data)
        await writer.drain()


async def _pipe(cr, cw, ur, uw) -> None:
    async def copy(src, dst):
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                if dst.can_write_eof():
                    dst.write_eof()

    a = asyncio.create_task(copy(cr, uw))
    b = asyncio.create_task(copy(ur, cw))
    done, pending = await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
    # One side finished: give the other a moment to drain, then close both.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait(pending, timeout=30)
    for t in pending:
        t.cancel()
