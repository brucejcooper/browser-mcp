"""The page limits: per site (registrable domain) and in all per day, and per
caller per hour, as sliding windows, kept in the state directory across
restarts. And the jittered spacing between page actions."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import tldextract

from .config import LimitsConfig
from .guard import Refused

DAY = 86400.0
HOUR = 3600.0

# The public suffix list bundled with tldextract; never fetched.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def site_of(host: str) -> str:
    """The registrable domain: www.example.co.uk -> example.co.uk."""
    r = _extract(host)
    return r.top_domain_under_public_suffix or host.lower().rstrip(".")


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
        self._last_action = 0.0
        self._load()

    def _prune(self, now: float) -> None:
        self.all = [t for t in self.all if t > now - DAY]
        for table, window in ((self.sites, DAY), (self.callers, HOUR)):
            for k in list(table):
                table[k] = [t for t in table[k] if t > now - window]
                if not table[k]:
                    del table[k]

    def check(self, caller: str, host: str) -> None:
        """Refused if one more page for this caller and site would pass a cap."""
        now = self.clock()
        self._prune(now)
        site = site_of(host)
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
        self._prune(self.clock())

    def _save(self) -> None:
        if not self.state_file:
            return
        self._prune(self.clock())
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"all": self.all, "sites": self.sites, "callers": self.callers}))
        tmp.replace(self.state_file)
