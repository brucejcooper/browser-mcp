"""The live view's server on its own, in front of a stand-in VNC server: the
one-time link, the session, the proxied mode's header, and nothing left
listening once it closes."""

import asyncio
import os
import socket

import httpx
import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from browser_mcp.config import ConfigError, ViewConfig, load
from browser_mcp.liveview import LiveView


def listening() -> set[int]:
    """The TCP ports listening here, as `ss -ltn` lists them."""
    ports = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        if not os.path.exists(name):
            continue
        with open(name) as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if parts[3] == "0A":
                    ports.add(int(parts[1].rsplit(":", 1)[1], 16))
    return ports


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def novnc(tmp_path):
    d = tmp_path / "novnc"
    (d / "core").mkdir(parents=True)
    (d / "core" / "rfb.js").write_text("export default class RFB {}")
    (tmp_path / "secret.txt").write_text("outside")
    return d


@pytest.fixture
def fake_vnc(monkeypatch):
    """x11vnc's stand-in: a Unix socket that greets as an RFB server does."""
    monkeypatch.setattr("browser_mcp.liveview.x11vnc", lambda: "/bin/false")
    servers = []

    async def start_vnc(self):
        import tempfile
        self._dir = tempfile.mkdtemp(prefix="browser-mcp-test-")

        async def greet(reader, writer):
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            data = await reader.read(12)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        servers.append(await asyncio.start_unix_server(greet, self.socket))

    monkeypatch.setattr(LiveView, "_start_vnc", start_vnc)
    yield
    for s in servers:
        s.close()


async def open_view(cfg: ViewConfig) -> LiveView:
    view = LiveView(cfg, ":99", "a hcaptcha challenge on example.com")
    await view.start()
    return view


async def test_local_mode_one_time_link(novnc, fake_vnc):
    before = listening()
    view = await open_view(ViewConfig(novnc_dir=str(novnc)))
    try:
        assert view.url == f"http://127.0.0.1:{view.port}/view/{view.token}/"
        assert listening() - before == {view.port}
        async with httpx.AsyncClient() as person, httpx.AsyncClient() as other:
            r = await person.get(view.url)
            assert r.status_code == 200 and "./novnc/core/rfb.js" in r.text and "example.com" in r.text
            nonce = r.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
            assert f'nonce="{nonce}"' in r.text
            assert r.headers["referrer-policy"] == "no-referrer"
            cookie = r.cookies["browser_mcp_view"]
            # A second open of the link, from anywhere else, is refused.
            r = await other.get(view.url)
            assert r.status_code == 403 and "already been used" in r.text
            # The same session may reload it.
            assert (await person.get(view.url)).status_code == 200
            # Another token is refused, and so is one that isn't ASCII, or a cookie that isn't.
            r = await person.get(f"http://127.0.0.1:{view.port}/view/{'x' * 43}/")
            assert r.status_code == 403
            assert (await other.get(f"http://127.0.0.1:{view.port}/view/%C3%A9/")).status_code == 403
            r = await other.get(view.url + "novnc/core/rfb.js", headers={"Cookie": "browser_mcp_view=\u00e9".encode()})
            assert r.status_code == 403
            # noVNC's files need the session, and stay inside noVNC's directory.
            assert (await person.get(view.url + "novnc/core/rfb.js")).status_code == 200
            assert (await other.get(view.url + "novnc/core/rfb.js")).status_code == 403
            assert (await person.get(view.url + "novnc/../secret.txt")).status_code in (403, 404)
            assert (await person.get(view.url + "novnc/%2e%2e/secret.txt")).status_code == 404
            assert (await other.post(view.url + "done")).status_code == 403
            assert not view.done.is_set()

            ws_url = view.url.replace("http:", "ws:") + "websockify"
            with pytest.raises(WebSocketException):
                async with ws_connect(ws_url, subprotocols=["binary"]) as ws:
                    await ws.recv()
            async with ws_connect(ws_url, subprotocols=["binary"],
                                          additional_headers={"Cookie": f"browser_mcp_view={cookie}"}) as ws:
                assert ws.subprotocol == "binary"
                assert await ws.recv() == b"RFB 003.008\n"
                await ws.send(b"RFB 003.008\n")
                assert await ws.recv() == b"echo:RFB 003.008\n"

            assert (await person.post(view.url + "done")).status_code == 200
            assert view.done.is_set()
            assert (await person.get(view.url)).status_code == 410
    finally:
        await view.stop()
    assert view.port not in listening()
    assert not os.path.exists(view.socket)


async def test_proxied_mode_needs_the_proxys_user_header(novnc, fake_vnc):
    port = free_port()
    cfg = ViewConfig(mode="proxied", host="127.0.0.1", port=port, public_url="https://view.example.org/",
                     user_header="X-Auth-User", novnc_dir=str(novnc))
    view = await open_view(cfg)
    try:
        assert view.url == f"https://view.example.org/view/{view.token}/"
        local = f"http://127.0.0.1:{port}/view/{view.token}/"
        async with httpx.AsyncClient() as c, httpx.AsyncClient() as other:
            # The token alone isn't enough.
            r = await c.get(local)
            assert r.status_code == 403 and "no authenticated user" in r.text
            assert view.session is None  # and it didn't use the link up
            r = await c.get(local, headers={"X-Auth-User": ""})
            assert r.status_code == 403
            # The header without the token isn't either.
            r = await c.get(f"http://127.0.0.1:{port}/view/wrong/", headers={"X-Auth-User": "alice"})
            assert r.status_code == 403
            r = await c.get(local, headers={"X-Auth-User": "alice"})
            assert r.status_code == 200 and view.user == "alice"
            assert "secure" in r.headers["set-cookie"].lower()
            # Another user, even with alice's cookie, is refused.
            r = await other.get(local, headers={"X-Auth-User": "mallory",
                                                "Cookie": f"browser_mcp_view={view.session}"})
            assert r.status_code == 403
    finally:
        await view.stop()
    assert port not in listening()


async def test_the_link_is_gone_once_it_closes(novnc, fake_vnc):
    view = await open_view(ViewConfig(novnc_dir=str(novnc)))
    url = view.url
    await view.stop()
    async with httpx.AsyncClient() as c:
        with pytest.raises(httpx.ConnectError):
            await c.get(url)


def test_proxied_mode_config_is_checked(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[view]\nmode = "proxied"\nport = 8090\nuser_header = "X-User"\n')
    with pytest.raises(ConfigError, match="public_url"):
        load(f, env={})
    f.write_text('[view]\nmode = "proxied"\nport = 8090\npublic_url = "https://v.example/"\n')
    with pytest.raises(ConfigError, match="user_header"):
        load(f, env={})
    f.write_text('[view]\nmode = "proxied"\npublic_url = "https://v.example/"\nuser_header = "X-User"\n')
    with pytest.raises(ConfigError, match="fixed port"):
        load(f, env={})
    f.write_text('[view]\nmode = "proxied"\nport = 8090\npublic_url = "https://example.org/view/"\n'
                 'user_header = "X-User"\n')
    with pytest.raises(ConfigError, match="root of its host"):
        load(f, env={})
    f.write_text('[view]\nmode = "remote"\n')
    with pytest.raises(ConfigError, match="local or proxied"):
        load(f, env={})
    assert load(None, env={"BROWSER_MCP_NOVNC_DIR": "/x"}).view.novnc_dir == "/x"
