"""The streamable HTTP transport: loopback by default, and a bearer token
required (and checked) on any other bind."""

import asyncio

import httpx
import pytest
import uvicorn

from browser_mcp.config import Config
from browser_mcp.server import build, http_app, is_loopback

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_loopback():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.0.2.1")


def test_a_non_loopback_bind_needs_a_token(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    cfg.http.host = "0.0.0.0"
    mcp, svc = build(cfg)
    with pytest.raises(SystemExit, match="needs a bearer token"):
        http_app(mcp, svc, cfg)
    short = tmp_path / "short"
    short.write_text("abc")
    cfg.http.token_file = str(short)
    with pytest.raises(SystemExit, match="at least 16"):
        http_app(mcp, svc, cfg)


async def serve(cfg):
    mcp, svc = build(cfg)
    server = uvicorn.Server(uvicorn.Config(http_app(mcp, svc, cfg), host=cfg.http.host, port=cfg.http.port,
                                           log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
async def test_the_bearer_is_checked(tmp_path, host):
    token = tmp_path / "token"
    token.write_text("0123456789abcdef0123\n")
    cfg = Config(state_dir=str(tmp_path))
    cfg.http.host, cfg.http.port, cfg.http.token_file = host, 0, str(token)
    cfg.metrics.enabled = True
    server, task, port = await serve(cfg)
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(url + "/mcp", json=INIT, headers=HEADERS)
            assert r.status_code == 401
            r = await c.post(url + "/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer wrong"})
            assert r.status_code == 401
            assert (await c.get(url + "/metrics")).status_code == 401
            ok = {**HEADERS, "Authorization": "Bearer 0123456789abcdef0123"}
            r = await c.post(url + "/mcp", json=INIT, headers=ok)
            assert r.status_code == 200 and "browser-mcp" in r.text
            r = await c.get(url + "/metrics", headers=ok)
            assert r.status_code == 200 and "browser_mcp_pages" in r.text
    finally:
        server.should_exit = True
        await task


async def test_loopback_without_a_token_lists_the_tools(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    cfg.http.port = 0
    server, task, port = await serve(cfg)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.post("/mcp", json=INIT, headers=HEADERS)
            assert r.status_code == 200
            session = {**HEADERS, "Mcp-Session-Id": r.headers["mcp-session-id"]}
            await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=session)
            names = sorted(t for t in ("browse", "search", "site_search") if f'"name":"{t}"' in r.text)
            assert names == ["browse", "search", "site_search"]
            assert "untrusted" in r.text
            for absent in ("click", "type", "fill"):
                assert f'"name":"{absent}"' not in r.text
    finally:
        server.should_exit = True
        await task
