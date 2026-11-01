"""What the browser may load: the URL deny list and the egress deny networks."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

from .config import DenyConfig


class Refused(Exception):
    """A request the server won't make; the message says why."""


Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class Guard:
    def __init__(self, cfg: DenyConfig):
        self.words = [w.lower() for w in cfg.words]
        word_re = "|".join(re.escape(w) for w in sorted(self.words, key=len, reverse=True))
        # A whole word: not inside a longer run of letters or digits.
        self._words = re.compile(rf"(?<![a-z0-9])(?:{word_re})(?![a-z0-9])") if self.words else None
        self._patterns = [re.compile(p, re.I) for p in cfg.patterns]
        self.networks = [ipaddress.ip_network(n, strict=False) for n in cfg.networks]
        self.unresolvable_via_proxy = cfg.unresolvable_via_proxy

    # The URL deny list.

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise Refused(f"only http and https URLs are allowed, not {parts.scheme or 'none'!r}")
        if not parts.hostname:
            raise Refused("the URL has no host")
        if parts.username or parts.password:
            raise Refused("URLs with credentials are refused")
        if self._words:
            where = f"{parts.hostname} {parts.path}".lower()
            m = self._words.search(where)
            if m:
                raise Refused(f"the URL is on the deny list ({m.group(0)!r}): no logins, accounts or purchases")
        for p in self._patterns:
            if p.search(url):
                raise Refused(f"the URL matches the deny pattern {p.pattern!r}")

    # The egress deny networks.

    def denied_address(self, addr: Address) -> bool:
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in n for n in self.networks if n.version == addr.version)

    async def resolve(self, host: str, port: int) -> list[Address]:
        """The host's addresses, every one checked; Refused if any is denied.

        Empty if it doesn't resolve (the caller decides)."""
        host = host.strip("[]")
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except socket.gaierror:
                return []
            addrs = []
            for info in infos:
                a = ipaddress.ip_address(info[4][0].split("%")[0])
                if a not in addrs:
                    addrs.append(a)
        for a in addrs:
            if self.denied_address(a):
                raise Refused(f"{host} resolves to {a}, in a denied network")
        return addrs

    async def check_host(self, host: str, port: int, via_proxy: bool) -> list[Address]:
        addrs = await self.resolve(host, port)
        if not addrs and not (via_proxy and self.unresolvable_via_proxy):
            raise Refused(f"{host} doesn't resolve, so it can't be checked against the deny networks")
        return addrs

    async def check(self, url: str, via_proxy: bool = False) -> None:
        """Both checks, before a navigation."""
        self.check_url(url)
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        await self.check_host(parts.hostname, port, via_proxy)
