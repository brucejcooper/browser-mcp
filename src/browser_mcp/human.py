"""Human-like input: curved mouse paths, wheel and scroll-bar scrolling, typing
with jitter, and a dwell after load. Small on purpose (see the README's survey
for why it isn't a library)."""

from __future__ import annotations

import asyncio
import math
import random

from playwright.async_api import Locator, Page


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1])


def path(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[float, float]]:
    """Points along a cubic Bézier from start to end, with control points off
    the straight line, eased in and out, and a little hand tremor."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    dist = math.hypot(dx, dy)
    if dist < 1:
        return [end]
    # Perpendicular offsets for the control points, up to a third of the way.
    nx, ny = -dy / dist, dx / dist
    spread = min(dist / 3, 200)
    c1 = (start[0] + dx * random.uniform(0.2, 0.4) + nx * random.uniform(-spread, spread),
          start[1] + dy * random.uniform(0.2, 0.4) + ny * random.uniform(-spread, spread))
    c2 = (start[0] + dx * random.uniform(0.6, 0.8) + nx * random.uniform(-spread, spread),
          start[1] + dy * random.uniform(0.6, 0.8) + ny * random.uniform(-spread, spread))
    # Fitts-like: longer moves take more steps, but not linearly.
    steps = max(8, min(60, int(math.log2(dist + 1) * random.uniform(3, 5))))
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # ease in and out
        x, y = _bezier(start, c1, c2, end, t)
        if i < steps:
            x += random.gauss(0, 0.6)
            y += random.gauss(0, 0.6)
        points.append((x, y))
    return points


class Human:
    def __init__(self, page: Page, width: int, height: int):
        self.page = page
        self.width = width
        self.height = height
        self.pos = (random.uniform(0.3, 0.7) * width, random.uniform(0.3, 0.6) * height)

    async def move(self, x: float, y: float) -> None:
        x = min(max(x, 1), self.width - 2)
        y = min(max(y, 1), self.height - 2)
        # Now and then, overshoot a little and come back.
        if random.random() < 0.15:
            over = (x + random.uniform(-12, 12), y + random.uniform(-8, 8))
            await self._along(path(self.pos, over))
        await self._along(path(self.pos, (x, y)))

    async def _along(self, points) -> None:
        for px, py in points:
            await self.page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.004, 0.018))
        self.pos = points[-1]

    async def wander(self, moves: int | None = None) -> None:
        """A few idle movements, as a reader's hand drifts."""
        for _ in range(moves if moves is not None else random.randint(1, 3)):
            await self.move(random.uniform(0.15, 0.85) * self.width, random.uniform(0.2, 0.8) * self.height)
            await asyncio.sleep(random.uniform(0.1, 0.5))

    async def click_at(self, x: float, y: float) -> None:
        await self.move(x, y)
        await asyncio.sleep(random.uniform(0.08, 0.25))
        await self.page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.14))
        await self.page.mouse.up()

    async def click(self, target: Locator) -> None:
        await target.scroll_into_view_if_needed(timeout=5000)
        box = await target.bounding_box()
        if not box:
            await target.click()
            return
        # Somewhere inside, nearer the middle than the edge.
        x = box["x"] + box["width"] * min(max(random.gauss(0.5, 0.15), 0.2), 0.8)
        y = box["y"] + box["height"] * min(max(random.gauss(0.5, 0.15), 0.25), 0.75)
        await self.click_at(x, y)

    async def type(self, target: Locator, text: str) -> None:
        await asyncio.sleep(random.uniform(0.4, 0.8))  # let a smooth scroll come to rest
        for _ in range(3):
            await self.click(target)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            if await target.evaluate("e => e === document.activeElement"):
                break
        else:
            await target.focus()  # something kept covering it
        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Backspace")
        for i, ch in enumerate(text):
            await self.page.keyboard.type(ch)
            delay = random.lognormvariate(math.log(0.11), 0.4)
            if ch == " " or random.random() < 0.05:
                delay += random.uniform(0.1, 0.35)  # a beat between words
            await asyncio.sleep(min(delay, 0.8))
        if (await target.input_value()).strip() != text.strip():
            await target.fill(text)  # the page rewrote it as we typed
        await asyncio.sleep(random.uniform(0.25, 0.7))

    async def scroll(self, amount: float | None = None) -> None:
        """Wheel down in uneven notches, with pauses."""
        total = amount if amount is not None else random.uniform(0.6, 1.6) * self.height
        done = 0.0
        while done < total:
            notch = random.choice((100, 100, 120, 200))
            await self.page.mouse.wheel(0, notch)
            done += notch
            await asyncio.sleep(random.uniform(0.04, 0.2))
        await asyncio.sleep(random.uniform(0.2, 0.6))

    async def page_down_by_scrollbar(self) -> bool:
        """Click the scroll bar's track below the thumb, as a person pages
        down. False if the page has no classic scroll bar to click."""
        info = await self.page.evaluate("""() => {
            const de = document.documentElement;
            const bar = window.innerWidth - de.clientWidth;
            const max = de.scrollHeight - window.innerHeight;
            return {bar, max, y: window.scrollY, h: window.innerHeight};
        }""")
        if info["bar"] < 6 or info["max"] <= info["y"] + 10:
            return False
        thumb_h = max(20, info["h"] * info["h"] / (info["h"] + info["max"]))
        thumb_top = (info["h"] - thumb_h) * info["y"] / info["max"]
        below = thumb_top + thumb_h + 8
        if below > info["h"] - 20:
            return False
        x = self.width - info["bar"] / 2
        await self.click_at(x, random.uniform(below, info["h"] - 20))
        await asyncio.sleep(random.uniform(0.3, 0.8))
        return True

    async def read(self) -> None:
        """The dwell after load: a look around, a scroll, sometimes a page
        down by the scroll bar on a long page."""
        await asyncio.sleep(random.uniform(0.6, 1.4))
        await self.wander()
        await self.scroll()
        if random.random() < 0.35:
            await self.page_down_by_scrollbar()
