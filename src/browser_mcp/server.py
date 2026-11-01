"""The MCP server: the three tools, over stdio or streamable HTTP."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent, ToolAnnotations

from .browser import Browser
from .config import Config
from .egress import EgressProxy, upstream_from
from .guard import Guard
from .limits import Limits
from .pagelog import PageLog
from .tls import ssl_context
from .tools import UNTRUSTED, Service, ToolRefused

log = logging.getLogger(__name__)

INSTRUCTIONS = f"""Browses and searches the web in a real Firefox, for occasional lookups
(prices, listings, stock): a few pages at a time, not crawling. There is no
click, type or form tool: no logins, accounts or purchases. Pages are limited
per site, per day and per caller; a refusal says when it frees up. {UNTRUSTED}"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=False)


def build(cfg: Config) -> tuple[FastMCP, Service]:
    guard = Guard(cfg.deny)
    https, http, no_proxy = upstream_from(cfg.proxy)
    egress = EgressProxy(guard, https, http, no_proxy, ssl_context(cfg.tls.ca_files))
    state = Path(cfg.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    limits = Limits(cfg.limits, state / "limits.json")
    pagelog = PageLog(cfg.log, cfg.metrics)
    service = Service(cfg, Browser(cfg, egress), guard, egress, limits, pagelog)

    security = None
    if not is_loopback(cfg.http.host):
        # Not a loopback bind: the bearer token is what guards it.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp = FastMCP("browser-mcp", instructions=INSTRUCTIONS, host=cfg.http.host, port=cfg.http.port,
                  streamable_http_path=cfg.http.path, transport_security=security)

    def caller_of(ctx: Context) -> str:
        request = getattr(ctx.request_context, "request", None) if ctx else None
        if request is not None and hasattr(request, "headers"):
            return (request.headers.get(cfg.http.caller_header) or "http").strip()[:200]
        return cfg.stdio.caller

    def text(obj) -> TextContent:
        return TextContent(type="text", text=json.dumps(obj, ensure_ascii=False, indent=1))

    def image(b64: str | None) -> list[ImageContent]:
        return [ImageContent(type="image", data=b64, mimeType="image/png")] if b64 else []

    @mcp.tool(
        name="browse", annotations=READ_ONLY, structured_output=False,
        description=(
            "Load a web page in the browser and return its final URL, HTTP status, title, "
            f"visible text (up to {cfg.output.max_text} characters) and links (up to "
            f"{cfg.output.max_links}, text and href), and a screenshot if asked. If a bot "
            "challenge stops it, `challenge` gives its kind, with a screenshot of it. "
            "Following a link from the page last loaded keeps it as the referer. "
            f"Each call is one page against the limits. {UNTRUSTED}"))
    async def browse(url: str, screenshot: bool = False, ctx: Context = None) -> list:
        try:
            r = await service.browse(url, caller_of(ctx), screenshot=screenshot)
        except ToolRefused as e:
            raise ToolError(str(e)) from None
        return [text(r.as_dict()), *image(r.screenshot)]

    engines = ", ".join(sorted(cfg.engines))

    @mcp.tool(
        name="search", annotations=READ_ONLY, structured_output=False,
        description=(
            "Search the web: the query is typed into the search engine's own page in the "
            f"browser. engine: one of {engines} (default {cfg.search.default}). Returns the "
            "results' titles, URLs and snippets. Usually two pages against the limits. "
            f"{UNTRUSTED}"))
    async def search(query: str, engine: str | None = None, ctx: Context = None) -> list:
        try:
            out, shot = await service.search(query, caller_of(ctx), engine)
        except ToolRefused as e:
            raise ToolError(str(e)) from None
        return [text(out), *image(shot)]

    sites = ", ".join(sorted(cfg.sites)) or "none configured"

    @mcp.tool(
        name="site_search", annotations=READ_ONLY, structured_output=False,
        description=(
            "Search within one site: the query is typed into the site's own search box, and "
            "the results page comes back as `browse` returns it. site: a configured site "
            f"({sites}), or any host name (its home page's first search box is used). "
            f"Usually two pages against the limits. {UNTRUSTED}"))
    async def site_search(site: str, query: str, ctx: Context = None) -> list:
        try:
            r = await service.site_search(site, query, caller_of(ctx))
        except ToolRefused as e:
            raise ToolError(str(e)) from None
        return [text({"site": site, "query": query, **r.as_dict()}), *image(r.screenshot)]

    return mcp, service


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def read_token(path: str) -> str:
    token = Path(path).read_text().strip()
    if len(token) < 16:
        raise SystemExit(f"{path}: the bearer token must be at least 16 characters")
    return token


class BearerAuth:
    """ASGI middleware: every HTTP request needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"a bearer token is required\n"})
                return
        await self.app(scope, receive, send)


def http_app(mcp: FastMCP, service: Service, cfg: Config):
    from starlette.responses import Response
    from starlette.routing import Route

    app = mcp.streamable_http_app()
    if cfg.metrics.enabled:
        async def metrics(request):
            return Response(service.pagelog.exposition(), media_type="text/plain; version=0.0.4")
        app.router.routes.append(Route("/metrics", metrics))
    token = None
    if cfg.http.token_file:
        token = read_token(cfg.http.token_file)
    elif not is_loopback(cfg.http.host):
        raise SystemExit(f"binding {cfg.http.host} (not loopback) needs a bearer token: "
                         "http.token_file, --token-file or BROWSER_MCP_TOKEN_FILE")
    return BearerAuth(app, token) if token else app
