"""Behind a TLS-inspecting proxy (mitmproxy, with its own root CA): browse and
search work when the CA is given (by flag, by env, by config file), fail with
a clear certificate error when it isn't, go through the proxy, and NO_PROXY
bypasses it."""

import asyncio
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from browser_mcp.cli import configure, parse_args
from browser_mcp.config import Engine
from browser_mcp.server import build
from browser_mcp.tools import ToolRefused
from test_browser import with_engine

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not all(shutil.which(t) for t in ("Xvfb", "certutil", "mitmdump", "openssl")),
                       reason="needs Xvfb, certutil, mitmdump and openssl"),
]

HERE = Path(__file__).parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Origin:
    """An HTTPS site on loopback with a self-signed certificate (only mitmproxy
    talks to it), with a search page."""

    def __init__(self, tmp: Path, tls: bool = True):
        page = b"<title>Origin</title><p>through the proxy</p>"
        search = b'<title>Engine</title><form action="/results"><input name="q"></form>'

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = search if self.path == "/engine" else (
                    b'<title>r</title><div class="r"><h3>Hit</h3><a href="https://hit.example/">x</a><p>found</p></div>'
                    if self.path.startswith("/results") else page)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        if tls:
            key, crt = tmp / "origin.key", tmp / "origin.crt"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                            "-out", crt, "-days", "2", "-subj", "/CN=127.0.0.1",
                            "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(crt, key)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.base = f"{'https' if tls else 'http'}://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture(scope="module")
def mitm(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mitm")
    port = free_port()
    log = tmp / "flows.log"
    log.touch()
    proc = subprocess.Popen(
        ["mitmdump", "--listen-host", "127.0.0.1", "--listen-port", str(port), "--set", f"confdir={tmp}",
         "--set", "ssl_insecure=true", "-s", str(HERE / "mitm_log.py"), "-q"],
        env={**os.environ, "MITM_LOG": str(log)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ca = tmp / "mitmproxy-ca-cert.pem"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            if ca.exists():
                break
        except OSError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("mitmdump didn't start")
    yield {"url": f"http://127.0.0.1:{port}", "ca": ca, "log": log}
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="module")
def origin(tmp_path_factory):
    o = Origin(tmp_path_factory.mktemp("origin"))
    yield o
    o.close()


def settings(cfg, origin):
    with_engine(cfg)
    cfg.deny.networks = []  # everything here is on loopback
    cfg.limits.delay_min = cfg.limits.delay_max = 0
    cfg.engines["local"] = Engine(url=origin.base + "/engine", input='input[name="q"]', result="div.r",
                                  title="h3", link="a", snippet="p")
    return cfg


async def run(cfg, origin):
    _, svc = build(cfg)
    try:
        page = await svc.browse(origin.base + "/page", "t")
        out, _ = await svc.search("ddr4", "t", "local")
        return page, out
    finally:
        await svc.browser.close()
        await svc.egress.stop()


def seen(mitm, url) -> bool:
    return url in mitm["log"].read_text()


async def test_ca_by_flag(tmp_path, mitm, origin, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", mitm["url"])  # the proxy from the environment
    cfg = settings(configure(parse_args(["--ca-file", str(mitm["ca"]), "--state-dir", str(tmp_path)]), env={}), origin)
    page, out = await run(cfg, origin)
    assert page.text == "through the proxy"
    assert out["results"] == [{"title": "Hit", "url": "https://hit.example/", "snippet": "found"}]
    assert seen(mitm, origin.base + "/page")


async def test_ca_by_env(tmp_path, mitm, origin):
    cfg = configure(parse_args(["--state-dir", str(tmp_path)]), env={"BROWSER_MCP_CA_FILES": str(mitm["ca"])})
    cfg = settings(cfg, origin)
    cfg.proxy.url = mitm["url"]
    page, out = await run(cfg, origin)
    assert page.text == "through the proxy" and out["results"]


async def test_ca_by_config_file(tmp_path, mitm, origin):
    conf = tmp_path / "config.toml"
    conf.write_text(f'state_dir = "{tmp_path}"\n[proxy]\nurl = "{mitm["url"]}"\n[tls]\nca_files = ["{mitm["ca"]}"]\n')
    cfg = settings(configure(parse_args(["--config", str(conf)]), env={}), origin)
    page, out = await run(cfg, origin)
    assert page.text == "through the proxy" and out["results"]


async def test_without_the_ca_a_clear_certificate_error(tmp_path, mitm, origin):
    cfg = settings(configure(parse_args(["--state-dir", str(tmp_path)]), env={}), origin)
    cfg.proxy.url = mitm["url"]
    _, svc = build(cfg)
    try:
        with pytest.raises(ToolRefused, match=r"certificate error \(\w+\).*--ca-file"):
            await svc.browse(origin.base + "/page", "t")
        # Off the error page, so it can't load the URL again under the next call.
        assert (await svc.browser.page()).url == "about:blank"
        with pytest.raises(ToolRefused, match=r"certificate error"):
            await svc.search("ddr4", "t", "local")
    finally:
        await svc.browser.close()
        await svc.egress.stop()


async def test_no_proxy_bypasses_the_proxy(tmp_path, mitm):
    plain = Origin(tmp_path, tls=False)
    try:
        for no_proxy, through in (("", True), (f"127.0.0.1:{plain.port}", False)):
            cfg = configure(parse_args(["--state-dir", str(tmp_path / str(through))]), env={})
            cfg.deny.networks = []
            cfg.limits.delay_min = cfg.limits.delay_max = 0
            cfg.proxy.url = mitm["url"]
            cfg.proxy.no_proxy = no_proxy
            url = f"{plain.base}/{'via' if through else 'direct'}"
            _, svc = build(cfg)
            try:
                r = await svc.browse(url, "t")
            finally:
                await svc.browser.close()
                await svc.egress.stop()
            assert r.text == "through the proxy"
            assert seen(mitm, url) is through
    finally:
        plain.close()
