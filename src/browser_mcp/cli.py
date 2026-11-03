"""browser-mcp: the command line."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import __version__
from .config import Config, ConfigError, load, validate


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="browser-mcp", description=__doc__)
    p.add_argument("--config", "-c", default=os.environ.get("BROWSER_MCP_CONFIG"),
                   help="the TOML config file (env BROWSER_MCP_CONFIG)")
    p.add_argument("--transport", choices=("stdio", "http"), help="stdio (default) or streamable HTTP")
    p.add_argument("--host", help="the HTTP bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="the HTTP port (default 8080)")
    p.add_argument("--token-file", help="the bearer token file; required for a bind other than loopback")
    p.add_argument("--ca-file", action="append", default=[], metavar="PATH",
                   help="an extra CA file to trust (PEM or DER; repeatable; env BROWSER_MCP_CA_FILES)")
    p.add_argument("--state-dir", help="the profile and the limits' counters (default ./state)")
    p.add_argument("--log-file", help="the page log, one JSON line per page (default stderr)")
    p.add_argument("--caller", help="the caller id for stdio (default 'stdio')")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def configure(args: argparse.Namespace, env: dict[str, str] | None = None) -> Config:
    """The config file, then the environment, then the command line."""
    cfg = load(args.config, env)
    if args.transport:
        cfg.transport = args.transport
    if args.host:
        cfg.http.host = args.host
    if args.port:
        cfg.http.port = args.port
    if args.token_file:
        cfg.http.token_file = args.token_file
    cfg.tls.ca_files += args.ca_file
    if args.state_dir:
        cfg.state_dir = args.state_dir
    if args.log_file:
        cfg.log.file = args.log_file
    if args.caller:
        cfg.stdio.caller = args.caller
    validate(cfg)
    return cfg


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = configure(args)
    except (ConfigError, OSError) as e:
        sys.exit(f"browser-mcp: config: {e}")

    from .server import build, http_app

    mcp, service = build(cfg)
    try:
        if cfg.transport == "stdio":
            asyncio.run(_stdio(mcp, service))
        else:
            app = http_app(mcp, service, cfg)
            asyncio.run(_http(app, service, cfg))
    except KeyboardInterrupt:
        pass


async def _stdio(mcp, service) -> None:
    try:
        await mcp.run_stdio_async()
    finally:
        await service.end_handoff()
        await service.browser.close()
        await service.egress.stop()


async def _http(app, service, cfg) -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=cfg.http.host, port=cfg.http.port,
                                           log_level="info", lifespan="on"))
    try:
        await server.serve()
    finally:
        await service.end_handoff()
        await service.browser.close()
        await service.egress.stop()


if __name__ == "__main__":
    main()
