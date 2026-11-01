"""One JSON line per page, and the optional Prometheus counters."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prometheus_client import CollectorRegistry, Counter, generate_latest, write_to_textfile

from .config import LogConfig, MetricsConfig


class PageLog:
    def __init__(self, log: LogConfig, metrics: MetricsConfig):
        self.path = Path(log.file) if log.file else None
        self.textfile = metrics.textfile or None
        self.registry = CollectorRegistry()
        self.pages = Counter("browser_mcp_pages", "Top-level page loads, by tool and outcome",
                             ["tool", "outcome"], registry=self.registry)
        self.challenges = Counter("browser_mcp_challenges", "Challenges seen, by kind",
                                  ["kind"], registry=self.registry)
        self.refusals = Counter("browser_mcp_refusals", "Requests refused, by reason",
                                ["reason"], registry=self.registry)
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def page(self, *, caller: str, tool: str, url: str, final_url: str | None = None,
             status: int | None = None, bytes: int | None = None, challenge: str | None = None,
             outcome: str, detail: str | None = None) -> None:
        entry: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "caller": caller, "tool": tool, "url": url, "final_url": final_url,
            "status": status, "bytes": bytes, "challenge": challenge, "outcome": outcome,
        }
        if detail:
            entry["detail"] = detail
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            if self.path:
                with self.path.open("a") as f:
                    f.write(line + "\n")
            else:
                print(line, file=sys.stderr, flush=True)
        if outcome.startswith("refused"):
            self.refusals.labels(outcome.removeprefix("refused:") or "other").inc()
        else:
            self.pages.labels(tool, outcome).inc()
        if challenge:
            self.challenges.labels(challenge).inc()
        if self.textfile:
            write_to_textfile(self.textfile, self.registry)

    def exposition(self) -> bytes:
        return generate_latest(self.registry)
