"""The page limits: per site (registrable domain) and in all per day, and per
caller per hour, as sliding windows, kept in the state directory across
restarts; the sites left alone for the day after a challenge that wasn't
solved, or a block page. And the spacing: the jittered gap between page
actions, and a site's minimum interval between its page loads."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable

import tldextract

from .config import LimitsConfig
from .guard import Refused

log = logging.getLogger(__name__)

DAY = 86400.0
HOUR = 3600.0

# The public suffix list bundled with tldextract; never fetched.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def site_of(host: str) -> str:
    """The registrable domain: www.example.co.uk -> example.co.uk."""
    r = _extract(host)
    return r.top_domain_under_public_suffix or host.lower().rstrip(".")


UNSOLVED = "a challenge there wasn't solved today"


class LimitExceeded(Refused):
    pass


class Limits:
    def __init__(self, cfg: LimitsConfig, state_file: Path | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.clock = clock
        self.state_file = state_file
        self.sites: dict[str, list[float]] = defaultdict(list)
        self.callers: dict[str, list[float]] = defaultdict(list)
        self.all: list[float] = []
        self.left_alone: dict[str, float] = {}  # site: until when
        self.left_alone_why: dict[str, str] = {}  # site: why
        self._last_action = 0.0
        self._load()

    def _prune(self, now: float) -> None:
        self.all = [t for t in self.all if t > now - DAY]
        for table, window in ((self.sites, DAY), (self.callers, HOUR)):
            for k in list(table):
                table[k] = [t for t in table[k] if t > now - window]
                if not table[k]:
                    del table[k]
        self.left_alone = {k: t for k, t in self.left_alone.items() if t > now}
        self.left_alone_why = {k: w for k, w in self.left_alone_why.items() if k in self.left_alone}

    def check(self, caller: str, host: str) -> None:
        """Refused if one more page for this caller and site would pass a cap."""
        now = self.clock()
        self._prune(now)
        site = site_of(host)
        if site in self.left_alone:
            when = datetime.fromtimestamp(self.left_alone[site], timezone.utc).isoformat(timespec="seconds")
            why = self.left_alone_why.get(site, UNSOLVED)
            raise LimitExceeded(f"{site} is left alone until {when} (in {int(self.left_alone[site] - now) + 1} s): "
                                f"{why}")
        for times, cap, window, what in (
            (self.callers.get(caller, []), self.cfg.per_caller_per_hour, HOUR, f"{self.cfg.per_caller_per_hour} pages an hour for caller {caller!r}"),
            (self.sites.get(site, []), self.cfg.per_site_per_day, DAY, f"{self.cfg.per_site_per_day} pages a day on {site}"),
            (self.all, self.cfg.per_day, DAY, f"{self.cfg.per_day} pages a day in all"),
        ):
            if len(times) >= cap:
                # The page that frees one up is the oldest that still counts.
                frees = (sorted(times)[len(times) - cap] + window) if cap else None
                when = (datetime.fromtimestamp(frees, timezone.utc).isoformat(timespec="seconds")
                        if frees else "never (the cap is 0)")
                wait = f" (in {int(frees - now) + 1} s)" if frees else ""
                raise LimitExceeded(f"over the limit of {what}: refused; it frees up at {when}{wait}")

    def record(self, caller: str, host: str) -> None:
        now = self.clock()
        self.all.append(now)
        self.sites[site_of(host)].append(now)
        self.callers[caller].append(now)
        self._save()

    def leave_alone(self, host: str, why: str = "") -> float:
        """No more pages on this site until the end of the (local) day."""
        now = self.clock()
        today = datetime.fromtimestamp(now).astimezone()
        midnight = datetime.combine(today.date() + timedelta(days=1), dtime(), today.tzinfo)
        until = midnight.timestamp()
        self.left_alone[site_of(host)] = until
        self.left_alone_why[site_of(host)] = why or UNSOLVED
        self._save()
        return until

    async def space(self, host: str, interval: float) -> None:
        """Wait until the site's last page load is at least interval seconds
        ago, so a run of calls can't burst on it."""
        times = self.sites.get(site_of(host))
        if interval <= 0 or not times:
            return
        wait = max(times) + interval - self.clock()
        if wait > 0:
            log.info("%s had a page %.0f s ago: waiting %.0f s (its minimum interval is %.0f s)",
                     site_of(host), interval - wait, wait, interval)
            await asyncio.sleep(wait)

    async def pace(self) -> None:
        """Wait out the jittered spacing since the last page action."""
        gap = random.uniform(self.cfg.delay_min, self.cfg.delay_max)
        wait = self._last_action + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_action = time.monotonic()

    def _load(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        self.all = [float(t) for t in data.get("all", [])]
        self.sites.update({k: [float(t) for t in v] for k, v in data.get("sites", {}).items()})
        self.callers.update({k: [float(t) for t in v] for k, v in data.get("callers", {}).items()})
        self.left_alone = {k: float(t) for k, t in data.get("left_alone", {}).items()}
        self.left_alone_why = {k: str(w) for k, w in data.get("left_alone_why", {}).items()}
        self._prune(self.clock())

    def _save(self) -> None:
        if not self.state_file:
            return
        self._prune(self.clock())
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"all": self.all, "sites": self.sites, "callers": self.callers,
                                   "left_alone": self.left_alone, "left_alone_why": self.left_alone_why}))
        tmp.replace(self.state_file)
