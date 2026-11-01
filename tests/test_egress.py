import asyncio
import base64

import pytest

from browser_mcp.config import DenyConfig, ProxyConfig
from browser_mcp.egress import REFUSED_HEADER, EgressProxy, NoProxy, upstream_from
from browser_mcp.guard import Guard


async def origin_server():
    """A plain HTTP server on loopback that says which path it got."""
    async def handle(r, w):
        head = await r.readuntil(b"\r\n\r\n")
        path = head.split(b" ")[1]
        body = b"hello " + path
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        await w.drain()
        w.close()
    s = await asyncio.start_server(handle, "127.0.0.1", 0)
    return s, s.sockets[0].getsockname()[1]


async def upstream_proxy(seen):
    """An upstream proxy that records each request head and answers CONNECT
    with 200 and then echoes, and GET with a fixed body."""
    async def handle(r, w):
        head = await r.readuntil(b"\r\n\r\n")
        seen.append(head.decode())
        if head.startswith(b"CONNECT"):
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await w.drain()
            data = await r.read(100)
            w.write(b"echo:" + data)
        else:
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nupstream")
        await w.drain()
        w.close()
    s = await asyncio.start_server(handle, "127.0.0.1", 0)
    return s, s.sockets[0].getsockname()[1]


async def request(port, data: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(data)
    await w.drain()
    out = await asyncio.wait_for(r.read(), 5)
    w.close()
    return out


def test_no_proxy_matching():
    n = NoProxy("example.com, .corp.example, 10.0.0.0/8, host.test:8080, ::1")
    assert n.bypass("example.com", 443)
    assert n.bypass("www.example.com", 443)
    assert not n.bypass("notexample.com", 443)
    assert n.bypass("a.corp.example", 80)
    assert n.bypass("10.1.2.3", 80)
    assert n.bypass("host.test", 8080) and not n.bypass("host.test", 443)
    assert n.bypass("[::1]", 80)
    assert NoProxy("*").bypass("anything", 1)
    assert not NoProxy("").bypass("anything", 1)


def test_upstream_from_env_and_auth(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    https, http, np = upstream_from(ProxyConfig(username="me", password_file=str(pw)),
                                    env={"HTTPS_PROXY": "http://proxy.test:3128", "no_proxy": "internal.test"})
    assert (https.host, https.port, https.tls) == ("proxy.test", 3128, False)
    assert https.auth == "Basic " + base64.b64encode(b"me:s3cret").decode()
    assert http is None
    assert np.bypass("internal.test", 443)
    https, _, _ = upstream_from(ProxyConfig(), env={"https_proxy": "http://u%40x:p@proxy.test:8080"})
    assert https.auth == "Basic " + base64.b64encode(b"u@x:p").decode()
    https, http, _ = upstream_from(ProxyConfig(url="https://proxy.test"), env={})
    assert https.tls and https.port == 443 and http.host == "proxy.test"


async def test_refuses_denied_networks_on_every_request():
    origin, port = await origin_server()
    egress = EgressProxy(Guard(DenyConfig()), None, None, NoProxy(""))
    await egress.start()
    try:
        out = await request(egress.port, f"GET http://127.0.0.1:{port}/x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 403")
        assert REFUSED_HEADER.encode() in out
        out = await request(egress.port, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 403")
        assert egress.refusals_since(0)[0][0] == "127.0.0.1"
    finally:
        await egress.stop()
        origin.close()


async def test_forwards_direct_when_allowed():
    origin, port = await origin_server()
    egress = EgressProxy(Guard(DenyConfig(networks=[])), None, None, NoProxy(""))
    await egress.start()
    try:
        out = await request(egress.port, f"GET http://127.0.0.1:{port}/page?q=1 HTTP/1.1\r\nHost: x\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"hello /page?q=1")
        r, w = await asyncio.open_connection("127.0.0.1", egress.port)
        w.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        w.write(b"GET /tunnel HTTP/1.1\r\n\r\n")
        assert (await asyncio.wait_for(r.read(), 5)).endswith(b"hello /tunnel")
        w.close()
    finally:
        await egress.stop()
        origin.close()


async def test_chains_to_the_upstream_with_auth_and_honours_no_proxy():
    seen = []
    up, up_port = await upstream_proxy(seen)
    origin, port = await origin_server()
    https, http, _ = upstream_from(ProxyConfig(url=f"http://me:pw@127.0.0.1:{up_port}"), env={})
    egress = EgressProxy(Guard(DenyConfig(networks=[])), https, http, NoProxy("localhost"))
    await egress.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", egress.port)
        w.write(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        w.write(b"ping")
        assert await asyncio.wait_for(r.read(), 5) == b"echo:ping"
        w.close()
        assert seen[0].startswith("CONNECT 127.0.0.1:443 ")
        assert "Proxy-Authorization: Basic " + base64.b64encode(b"me:pw").decode() in seen[0]

        out = await request(egress.port, b"GET http://127.0.0.1/a HTTP/1.1\r\nHost: 127.0.0.1\r\nProxy-Authorization: Basic leak\r\n\r\n")
        assert out.endswith(b"upstream")
        assert seen[1].startswith("GET http://127.0.0.1/a ") and "leak" not in seen[1]

        # NO_PROXY: straight to the origin, the upstream never sees it.
        out = await request(egress.port, f"GET http://localhost:{port}/direct HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        assert out.endswith(b"hello /direct")
        assert len(seen) == 2
    finally:
        await egress.stop()
        up.close()
        origin.close()


async def test_upstream_errors_go_back_to_the_browser():
    async def handle(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
        w.close()
    up = await asyncio.start_server(handle, "127.0.0.1", 0)
    https, http, _ = upstream_from(ProxyConfig(url=f"http://127.0.0.1:{up.sockets[0].getsockname()[1]}"), env={})
    egress = EgressProxy(Guard(DenyConfig(networks=[])), https, http, NoProxy(""))
    await egress.start()
    try:
        out = await request(egress.port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 407")
    finally:
        await egress.stop()
        up.close()
