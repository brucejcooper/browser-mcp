"""The engine: Firefox, headful under an Xvfb of its own, driven by Playwright,
with one persistent profile and one tab. Started on demand; closed after an
idle time, the profile kept."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from . import tls
from .config import Config
from .egress import EgressProxy

log = logging.getLogger(__name__)

# navigator.webdriver as a person's Firefox has it: false, from a getter that
# still looks native (a Proxy of the original keeps its toString).
HIDE_WEBDRIVER = """(() => {
  const proto = Object.getPrototypeOf(navigator);
  const desc = Object.getOwnPropertyDescriptor(proto, 'webdriver');
  if (!desc || !desc.get) return;
  const get = new Proxy(desc.get, { apply: () => false });
  Object.defineProperty(proto, 'webdriver', { ...desc, get });
})();"""

# Prefs for the browser's egress and for looking like a person's Firefox.
BASE_PREFS = {
    # Everything through the egress proxy, loopback included.
    "network.proxy.allow_hijacking_localhost": True,
    "network.proxy.no_proxies_on": "",
    "network.proxy.socks_remote_dns": True,
    # WebRTC only through the proxy: no UDP around the egress checks.
    "media.peerconnection.ice.proxy_only": True,
    # No DNS over HTTPS around the egress checks, no DNS prefetch.
    "network.trr.mode": 5,
    "network.dns.disablePrefetch": True,
    # Classic scroll bars, so a page can be paged down by clicking the track.
    "widget.gtk.overlay-scrollbars.enabled": False,
    "widget.non-native-theme.scrollbar.style": 4,
    # No first-run pages, no telemetry, no update checks.
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "datareporting.policy.dataSubmissionEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "app.update.enabled": False,
}


class Xvfb:
    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.proc: subprocess.Popen | None = None
        self.display = ""

    def start(self) -> str:
        exe = shutil.which("Xvfb")
        if not exe:
            raise RuntimeError("Xvfb is needed to run Firefox headful (or set browser.display)")
        r, w = os.pipe()
        self.proc = subprocess.Popen(
            [exe, "-displayfd", str(w), "-screen", "0", f"{self.width}x{self.height}x24",
             "-nolisten", "tcp", "-dpi", "96", "+extension", "RANDR"],
            pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(w)
        with os.fdopen(r) as f:
            num = f.readline().strip()
        if not num:
            raise RuntimeError("Xvfb didn't start")
        self.display = f":{num}"
        return self.display

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(5)
        self.proc = None


def _window_size(profile: Path, width: int, height: int) -> None:
    """The window fills the screen, as a maximised one would: Firefox keeps
    its size in xulstore.json, and a new profile starts smaller."""
    path = profile / "xulstore.json"
    try:
        store = json.loads(path.read_text())
    except (OSError, ValueError):
        store = {}
    store.setdefault("chrome://browser/content/browser.xhtml", {})["main-window"] = {
        "screenX": "0", "screenY": "0", "width": str(width), "height": str(height), "sizemode": "normal"}
    profile.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store))


class Browser:
    def __init__(self, cfg: Config, egress: EgressProxy):
        self.cfg = cfg
        self.egress = egress
        self.profile = Path(cfg.state_dir) / "profile"
        self.lock = asyncio.Lock()  # one call at a time, one tab
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._xvfb: Xvfb | None = None
        self._last_used = time.monotonic()
        self._idle_task: asyncio.Task | None = None
        self.viewport = (cfg.browser.screen_width, cfg.browser.screen_height)
        # Links of the page the tab is on, for the referer when following one.
        self.current_links: set[str] = set()
        self.last_response = None

    @property
    def running(self) -> bool:
        return self._ctx is not None

    @property
    def own_display(self) -> str | None:
        """The display of the server's own Xvfb, which shows only this browser."""
        return self._xvfb.display if self._xvfb is not None and self.running else None

    async def page(self) -> Page:
        self._last_used = time.monotonic()
        if self._page is None or self._page.is_closed():
            await self._start()
        return self._page

    async def _start(self) -> None:
        await self.close()
        b = self.cfg.browser
        env = dict(os.environ)
        if not b.headless:
            if b.display:
                env["DISPLAY"] = b.display
            else:
                self._xvfb = Xvfb(b.screen_width, b.screen_height)
                env["DISPLAY"] = await asyncio.to_thread(self._xvfb.start)
        if not self.egress.port:
            await self.egress.start()
        await asyncio.to_thread(tls.install_into_profile, self.profile, self.cfg.tls.ca_files)
        _window_size(self.profile, b.screen_width, b.screen_height)
        self._pw = await async_playwright().start()
        prefs = {**BASE_PREFS, **b.firefox_prefs}
        try:
            self._ctx = await self._pw.firefox.launch_persistent_context(
                str(self.profile),
                headless=b.headless,
                no_viewport=not b.headless,
                viewport={"width": b.screen_width, "height": b.screen_height - 120} if b.headless else None,
                screen={"width": b.screen_width, "height": b.screen_height} if b.headless else None,
                locale=b.locale,
                timezone_id=b.timezone or None,
                proxy={"server": f"http://127.0.0.1:{self.egress.port}"},
                firefox_user_prefs=prefs,
                env=env,
                accept_downloads=False,
            )
        except Exception:
            await self.close()
            raise
        await self._ctx.add_init_script(HIDE_WEBDRIVER)
        self._ctx.set_default_navigation_timeout(b.navigation_timeout * 1000)
        self._ctx.set_default_timeout(15_000)
        pages = self._ctx.pages
        self._page = pages[0] if pages else await self._ctx.new_page()
        for extra in pages[1:]:
            await extra.close()
        # The newest top-level response, whatever navigated (a redirect by
        # script, an interstitial clearing itself).
        self._page.on("response", self._on_response)
        # One tab: a popup or a target=_blank is closed as it opens.
        self._ctx.on("page", lambda p: asyncio.ensure_future(p.close()) if p is not self._page else None)
        size = await self._page.evaluate("() => [window.innerWidth, window.innerHeight]")
        self.viewport = (size[0], size[1])
        self.current_links = set()
        log.info("browser started (profile %s, viewport %sx%s)", self.profile, *self.viewport)
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch())

    def _on_response(self, r) -> None:
        if r.request.is_navigation_request() and r.frame == self._page.main_frame:
            self.last_response = r

    async def _idle_watch(self) -> None:
        timeout = self.cfg.browser.idle_timeout
        while self.running:
            await asyncio.sleep(min(30.0, max(timeout / 4, 1.0)))
            if self.lock.locked():
                continue
            if time.monotonic() - self._last_used > timeout:
                async with self.lock:
                    if time.monotonic() - self._last_used > timeout:
                        log.info("browser idle for %ss: closing it (the profile is kept)", int(timeout))
                        await self.close()

    async def close(self) -> None:
        if self._ctx is not None:
            with contextlib.suppress(Exception):
                await self._ctx.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
        if self._xvfb is not None:
            await asyncio.to_thread(self._xvfb.stop)
        self._ctx = self._pw = self._page = self._xvfb = None

    def touch(self) -> None:
        self._last_used = time.monotonic()
