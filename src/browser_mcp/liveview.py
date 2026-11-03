"""The live view a person solves a challenge in: x11vnc on the browser's own
Xvfb, listening on a Unix socket only, and a small web server in front of it
with noVNC's page, a WebSocket bridge to the socket and a Done button. Both
run only while a hand-off is open.

The link carries a one-time token, good for this hand-off and for one
session: the first open of the page takes it and sets a cookie, and every
other request needs that cookie. In `proxied` mode every request also needs
the header the authenticating proxy names its user in."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import ViewConfig

log = logging.getLogger(__name__)

COOKIE = "browser_mcp_view"
PAGE = (Path(__file__).parent / "view.html").read_text()
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",  # the token is in the URL
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}
CSP = ("default-src 'self'; script-src 'self' 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data: blob:; "
       "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class Unavailable(Exception):
    """The live view can't run here; the message says why."""


def novnc_dir(cfg: ViewConfig) -> Path:
    d = Path(cfg.novnc_dir) if cfg.novnc_dir else None
    if d is None or not (d / "core" / "rfb.js").is_file():
        raise Unavailable("noVNC's files weren't found: set view.novnc_dir or BROWSER_MCP_NOVNC_DIR "
                          "to the directory holding core/rfb.js")
    return d


def x11vnc() -> str:
    exe = shutil.which("x11vnc")
    if not exe:
        raise Unavailable("x11vnc isn't installed (the Nix package and the Docker image have it)")
    return exe


def _same(given: str, expected: str) -> bool:
    """A constant-time comparison that takes any text (compare_digest refuses non-ASCII str)."""
    return hmac.compare_digest(given.encode(), expected.encode())


class _Server(uvicorn.Server):
    """The view's server inside this process: the signals stay the main loop's."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class LiveView:
    def __init__(self, cfg: ViewConfig, display: str, describe: str = "a challenge"):
        self.cfg = cfg
        self.display = display
        self.describe = describe
        self.novnc = novnc_dir(cfg)
        self.x11vnc = x11vnc()
        self.token = secrets.token_urlsafe(32)
        self.session: str | None = None  # the cookie, once the link has been opened
        self.user: str | None = None  # proxied: who opened it, as the proxy says
        self.done = asyncio.Event()  # the person pressed Done
        self.closed = asyncio.Event()
        self.expires = time.time() + cfg.timeout
        self.url = ""
        self.port = 0
        self._dir: str | None = None
        self._vnc: asyncio.subprocess.Process | None = None
        self._server: _Server | None = None
        self._task: asyncio.Task | None = None
        self._bridge: asyncio.Task | None = None

    @property
    def socket(self) -> str:
        return os.path.join(self._dir or "", "vnc")

    async def start(self) -> str:
        """Start x11vnc and the view's server; returns the link."""
        try:
            await self._start_vnc()
            await self._start_server()
        except BaseException:
            await self.stop()
            raise
        base = f"http://127.0.0.1:{self.port}" if self.cfg.mode == "local" else self.cfg.public_url.rstrip("/")
        self.url = f"{base}/view/{self.token}/"
        log.info("live view open on port %s (%s mode) until %s", self.port, self.cfg.mode,
                 time.strftime("%H:%M:%S", time.localtime(self.expires)))
        return self.url

    async def _start_vnc(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="browser-mcp-view-")  # 0700: only this user reaches the socket
        # -rfbport 0 and -rfbportv6 -1: no TCP at all, the Unix socket only.
        # -nosel: no clipboard either way.
        self._vnc = await asyncio.create_subprocess_exec(
            self.x11vnc, "-display", self.display, "-unixsock", self.socket, "-rfbport", "0",
            "-rfbportv6", "-1", "-nopw", "-forever", "-shared", "-nosel", "-noxdamage", "-nobell",
            "-quiet", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(self.socket):
                return
            if self._vnc.returncode is not None:
                break
            await asyncio.sleep(0.1)
        raise RuntimeError("x11vnc didn't start")

    async def _start_server(self) -> None:
        host = "127.0.0.1" if self.cfg.mode == "local" else self.cfg.host
        app = Starlette(routes=[
            Route("/view/{token}", self.redirect),
            Route("/view/{token}/", self.page),
            Route("/view/{token}/novnc/{path:path}", self.static),
            Route("/view/{token}/done", self.press_done, methods=["POST"]),
            WebSocketRoute("/view/{token}/websockify", self.websocket),
        ])
        self._server = _Server(uvicorn.Config(app, host=host, port=self.cfg.port, log_level="warning",
                                              lifespan="off", ws="wsproto", access_log=False,
                                              timeout_graceful_shutdown=2))
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()  # its error, e.g. the port is taken
                raise RuntimeError("the live view's server didn't start")
            await asyncio.sleep(0.05)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._bridge is not None:
            self._bridge.cancel()
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 5)
        if self._vnc is not None and self._vnc.returncode is None:
            self._vnc.terminate()
            try:
                await asyncio.wait_for(self._vnc.wait(), 5)
            except asyncio.TimeoutError:
                self._vnc.kill()
                await self._vnc.wait()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._server = self._task = self._vnc = self._bridge = None
        self.closed.set()

    # The checks every request passes.

    def _refusal(self, conn: HTTPConnection, need_session: bool = True) -> Response | None:
        if self.cfg.mode == "proxied":
            user = (conn.headers.get(self.cfg.user_header) or "").strip()
            if not user:
                log.warning("live view: a request without %s refused", self.cfg.user_header)
                return PlainTextResponse("refused: no authenticated user\n", 403, headers=HEADERS)
            if self.user is not None and user != self.user:
                return PlainTextResponse("refused: this view is another user's\n", 403, headers=HEADERS)
        if self.closed.is_set() or self.done.is_set() or time.time() > self.expires:
            return PlainTextResponse("this view has closed\n", 410, headers=HEADERS)
        if not _same(conn.path_params.get("token", ""), self.token):
            return PlainTextResponse("refused: not this view's link\n", 403, headers=HEADERS)
        if need_session and not (self.session and _same(conn.cookies.get(COOKIE, ""), self.session)):
            return PlainTextResponse("refused: this link has already been used\n", 403, headers=HEADERS)
        return None

    async def redirect(self, request: Request) -> Response:
        return self._refusal(request, need_session=False) or RedirectResponse(f"/view/{self.token}/", 307,
                                                                              headers=HEADERS)

    async def page(self, request: Request) -> Response:
        first = self.session is None
        refused = self._refusal(request, need_session=not first)
        if refused:
            log.warning("live view: page refused (%s)", refused.status_code)
            return refused
        if first:
            self.session = secrets.token_urlsafe(32)
            if self.cfg.mode == "proxied":
                self.user = request.headers.get(self.cfg.user_header).strip()
            log.info("live view opened%s", f" by {self.user}" if self.user else "")
        nonce = secrets.token_urlsafe(16)
        html = (PAGE.replace("{{EXPIRES}}", str(int(self.expires * 1000))).replace("{{NONCE}}", nonce)
                .replace("{{WHAT}}", self.describe.replace("&", "&amp;").replace("<", "&lt;")))
        response = HTMLResponse(html, headers={**HEADERS, "Content-Security-Policy": CSP.format(nonce=nonce)})
        response.set_cookie(COOKIE, self.session, path=f"/view/{self.token}/", httponly=True, samesite="strict",
                            secure=self.cfg.mode == "proxied" and self.cfg.public_url.startswith("https:"))
        return response

    async def static(self, request: Request) -> Response:
        refused = self._refusal(request)
        if refused:
            return refused
        root = self.novnc.resolve()
        path = (root / request.path_params["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return PlainTextResponse("not found\n", 404, headers=HEADERS)
        return FileResponse(path, headers=HEADERS)

    async def press_done(self, request: Request) -> Response:
        refused = self._refusal(request)
        if refused:
            return refused
        log.info("live view: Done pressed")
        self.done.set()
        return PlainTextResponse("done\n", headers=HEADERS)

    async def websocket(self, ws: WebSocket) -> None:
        refused = self._refusal(ws)
        if refused:
            await ws.close(code=1008)
            return
        if self._bridge is not None:
            self._bridge.cancel()  # a reload: the newer connection wins
        self._bridge = asyncio.current_task()
        offered = ws.scope.get("subprotocols") or []
        await ws.accept(subprotocol="binary" if "binary" in offered else None)
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket)
        except OSError:
            await ws.close(code=1011)
            return

        async def up() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                writer.write(msg.get("bytes") or (msg.get("text") or "").encode())
                await writer.drain()

        async def down() -> None:
            while data := await reader.read(65536):
                await ws.send_bytes(data)

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            writer.close()
            with contextlib.suppress(Exception):
                await ws.close()
            with contextlib.suppress(WebSocketDisconnect, Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._bridge is asyncio.current_task():
                self._bridge = None
