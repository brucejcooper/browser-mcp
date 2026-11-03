"""Bot challenges: detect them, wait out interstitials, click a checkbox
widget, and report anything else. And find the widget a challenge is
solved in, which is all `challenge()` may act on."""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from dataclasses import dataclass

from playwright.async_api import ElementHandle, Frame, Page, Response

from .human import Human

log = logging.getLogger(__name__)

# What the page says about itself; evaluated in the page.
_DETECT_JS = """() => {
  const t = (document.title || '').toLowerCase();
  const body = (document.body ? document.body.innerText : '').slice(0, 4000).toLowerCase();
  const has = s => !!document.querySelector(s);
  return {
    title: t, body,
    cf_form: has('#challenge-form, #challenge-running, #challenge-stage, form#challenge-form, div#cf-challenge-running, #cf-please-wait'),
    cf_turnstile: has('.cf-turnstile, [name="cf-turnstile-response"], #turnstile-wrapper'),
    px: has('#px-captcha, [id^="px-captcha"]'),
    dd: has('iframe[src*="captcha-delivery.com"]'),
    // A widget that has been solved: its response token is filled in.
    solved: [...document.querySelectorAll('[name="h-captcha-response"], [name="g-recaptcha-response"], [name="cf-turnstile-response"]')]
      .some(e => e.value),
  };
}"""


@dataclass
class Challenge:
    kind: str
    interstitial: bool = False  # it clears itself, given time
    checkbox: bool = False  # a widget with a checkbox to click


def _frame_kind(url: str) -> str | None:
    if "challenges.cloudflare.com" in url:
        return "turnstile"
    if "hcaptcha.com" in url:
        return "hcaptcha"
    if "/recaptcha/" in url and ("google.com" in url or "recaptcha.net" in url):
        return "recaptcha"
    if "captcha-delivery.com" in url:
        return "datadome"
    return None


async def detect(page: Page, response: Response | None) -> Challenge | None:
    headers = {}
    status = None
    if response is not None and response.url.split("#")[0] != page.url.split("#")[0]:
        response = None  # the page has moved on since: its headers say nothing now
    if response is not None:
        headers = {k.lower(): v for k, v in (await response.all_headers()).items()}
        status = response.status
    try:
        s = await page.evaluate(_DETECT_JS)
    except Exception:  # the page is navigating away: look again later
        return None
    frames = [k for f in page.frames if (k := _frame_kind(f.url))]
    title, body = s["title"], s["body"]

    if headers.get("cf-mitigated") == "challenge" or s["cf_form"] or title in (
            "just a moment...", "just a moment") or "checking your browser" in body or \
            "verify you are human" in body and "cloudflare" in body:
        if "turnstile" in frames or s["cf_turnstile"]:
            return Challenge("cloudflare_turnstile", checkbox=True)
        return Challenge("cloudflare_interstitial", interstitial=True)
    if "datadome" in frames or s["dd"] or headers.get("x-datadome") and status in (401, 403):
        return Challenge("datadome")
    if s["px"] or "press & hold" in body or "press and hold" in body and "human" in body:
        return Challenge("perimeterx")
    if status == 403 and ("akamaighost" in headers.get("server", "").lower() or (
            title == "access denied" and "reference #" in body)):
        return Challenge("akamai_block")
    # A widget on an ordinary page (reCAPTCHA v3 on every page, a form's
    # hCaptcha) is not a challenge: only a visible one on a page that is
    # mostly the challenge.
    blocking = status in (401, 403, 429, 503) or len(body) < 1500 or any(
        w in body for w in ("verify you are human", "are you a robot", "not a robot",
                            "unusual traffic", "complete the security check"))
    if s["solved"]:
        return None  # a solved widget: the page is the site's again
    if blocking:
        for kind in ("turnstile", "hcaptcha", "recaptcha"):
            if kind in frames and await _visible_widget(page, kind):
                return Challenge(kind, checkbox=True)
    if status in (403, 429) and any(w in body for w in ("captcha", "are you a robot", "not a robot", "unusual traffic")):
        return Challenge("block_page")
    return None


async def _visible_widget(page: Page, kind: str) -> bool:
    for f in _widget_frames(page, kind):
        try:
            box = await (await f.frame_element()).bounding_box()
        except Exception:
            continue
        if box and box["width"] >= 20 and box["height"] >= 20:
            return True
    return False


# The checkbox inside each widget's iframe, and where it sits if the frame
# can't be looked into (offsets from the iframe's top left).
_CHECKBOX = {
    "turnstile": ('input[type="checkbox"], .ctp-checkbox-label, label', (30, 32)),
    "hcaptcha": ("#checkbox", (30, 39)),
    "recaptcha": ("#recaptcha-anchor", (27, 39)),
}


def _widget_frames(page: Page, kind: str) -> list[Frame]:
    want = "turnstile" if "turnstile" in kind else kind
    out = []
    for f in page.frames:
        if _frame_kind(f.url) != want:
            continue
        if want == "recaptcha" and "anchor" not in f.url:
            continue
        if want == "hcaptcha" and "checkbox" not in f.url:
            continue
        out.append(f)
    return out


async def _click_checkbox(page: Page, human: Human, ch: Challenge) -> bool:
    want = "turnstile" if "turnstile" in ch.kind else ch.kind
    selector, (ox, oy) = _CHECKBOX[want]
    for frame in _widget_frames(page, ch.kind):
        try:
            el = await frame.frame_element()
            box = await el.bounding_box()
        except Exception:
            continue
        if not box or box["width"] < 20 or box["height"] < 20:
            continue  # an invisible widget: nothing to click
        target = None
        try:
            inner = frame.locator(selector).first
            if await inner.count():
                target = await inner.bounding_box()
        except Exception:
            target = None
        if target:
            x = target["x"] + target["width"] * random.uniform(0.35, 0.65)
            y = target["y"] + target["height"] * random.uniform(0.35, 0.65)
        else:
            x, y = box["x"] + ox + random.uniform(-3, 3), box["y"] + oy + random.uniform(-3, 3)
        await human.wander(1)
        await human.click_at(x, y)
        return True
    return False


# What each kind of challenge is solved in: frames of a widget's kind, or an
# element on the page itself. The rest (interstitials, block pages) have none.
_WIDGET_FRAMES = {"cloudflare_turnstile": "turnstile", "turnstile": "turnstile", "hcaptcha": "hcaptcha",
                  "recaptcha": "recaptcha", "datadome": "datadome"}
_WIDGET_ELEMENTS = {"perimeterx": "#px-captcha"}


@dataclass
class Widget:
    frame: Frame | None  # None: an element of the page itself
    element: ElementHandle
    box: dict[str, float]  # in the viewport, CSS pixels


async def widget(page: Page, ch: Challenge) -> Widget | None:
    """The widget the challenge is solved in, scrolled into view: the largest
    visible one, so an image challenge's frame once it has opened, else the
    checkbox's. None if the challenge has no widget."""
    candidates: list[tuple[Frame | None, ElementHandle]] = []
    if ch.kind in _WIDGET_FRAMES:
        want = _WIDGET_FRAMES[ch.kind]
        for f in page.frames:
            if f is not page.main_frame and _frame_kind(f.url) == want:
                try:
                    candidates.append((f, await f.frame_element()))
                except Exception:
                    continue
    elif ch.kind in _WIDGET_ELEMENTS:
        el = await page.query_selector(_WIDGET_ELEMENTS[ch.kind])
        if el:
            candidates.append((None, el))
    best, area = None, 0.0
    sx, sy = await page.evaluate("() => [scrollX, scrollY]")
    for frame, el in candidates:
        try:
            if not await el.is_visible():
                continue
            box = await el.bounding_box()
        except Exception:
            continue
        if not box or box["width"] < 20 or box["height"] < 20:
            continue
        # Off the page's top or left edge: parked out of sight, as reCAPTCHA
        # parks its image frame until it is needed.
        if box["x"] + sx + box["width"] <= 0 or box["y"] + sy + box["height"] <= 0:
            continue
        if box["width"] * box["height"] > area:
            best, area = (frame, el), box["width"] * box["height"]
    if best is None:
        return None
    frame, el = best
    try:
        await el.scroll_into_view_if_needed(timeout=5000)
        box = await el.bounding_box()
    except Exception:
        return None
    return Widget(frame, el, box) if box else None


async def widget_screenshot(page: Page, ch: Challenge) -> str | None:
    """A PNG of the widget (the whole viewport if there isn't one), base64."""
    try:
        for frame in _widget_frames(page, ch.kind) or [f for f in page.frames if _frame_kind(f.url)]:
            el = await frame.frame_element()
            if await el.is_visible():
                return base64.b64encode(await el.screenshot(timeout=5000)).decode()
        return base64.b64encode(await page.screenshot(timeout=5000)).decode()
    except Exception:
        return None


async def handle(page: Page, response: Response | None, human: Human,
                 interstitial_wait: float = 25.0, checkbox_wait: float = 15.0) -> tuple[Challenge | None, Response | None]:
    """Detect a challenge and try the trivial ways past it. Returns what's
    left (None when the page is clear) and the newest main response."""
    latest = response

    def on_response(r: Response) -> None:
        nonlocal latest
        if r.request.is_navigation_request() and r.frame == page.main_frame:
            latest = r

    page.on("response", on_response)
    try:
        ch = await detect(page, response)
        clicked = False
        deadline = time.monotonic() + interstitial_wait
        while ch is not None:
            if ch.checkbox and not clicked:
                await asyncio.sleep(random.uniform(1.5, 3.0))  # let the widget settle
                clicked = await _click_checkbox(page, human, ch)
                log.info("challenge %s: checkbox %s", ch.kind, "clicked" if clicked else "not found")
                deadline = time.monotonic() + checkbox_wait
            elif not ch.interstitial and not clicked:
                return ch, latest  # nothing to wait for
            if time.monotonic() > deadline:
                return ch, latest
            await asyncio.sleep(1.0)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            ch = await detect(page, latest)
        return None, latest
    finally:
        page.remove_listener("response", on_response)
