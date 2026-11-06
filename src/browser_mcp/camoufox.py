"""Camoufox's fingerprint (browser.engine = "camoufox"): what its engine tells
pages, given it in CAMOU_CONFIG, which Camoufox reads itself.

It claims the machine it really runs on: Linux desktop Firefox of Camoufox's
own version, the screen of its Xvfb, the fonts of the package's fontconfig,
the WebGL of Mesa's llvmpipe. Those are the host's, so nothing is spoofed that
a page could catch contradicting itself (a Windows profile over a Linux TCP
stack and llvmpipe would be), and no value is drawn at random: no canvas,
audio or font-metric noise, which stock Firefox doesn't have. What it sets is
what the host doesn't decide: the locale and the timezone, from the config.

Written to the state dir on first start and reused, so it stays the same
across restarts; made again, and logged, when what it is made from changes.
browser.camoufox_config is merged over it at launch."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import BrowserConfig

log = logging.getLogger(__name__)

FILE = "camoufox-fingerprint.json"

# Camoufox reads its config from CAMOU_CONFIG_1, _2, ... joined, each at most
# this long (an environment variable's limit on Linux).
CHUNK = 32767

# Prefs Camoufox's own launcher sets for a Linux profile.
PREFS = {
    # Per-character font fallback scans the font list synchronously: with it
    # async, the first measurement of a character only a fallback font has
    # comes back as the primary font's missing glyph, as stock Firefox's doesn't.
    "gfx.font_rendering.fallback.async": False,
}


def _inputs(b: BrowserConfig) -> dict[str, Any]:
    """What the fingerprint is made from: when one changes, it is made again."""
    return {"screen": [b.screen_width, b.screen_height], "locale": b.locale, "timezone": b.timezone}


def _make(b: BrowserConfig) -> dict[str, Any]:
    lang, _, region = b.locale.partition("-")
    config: dict[str, Any] = {"locale:language": lang}
    if region:
        config["locale:region"] = region
    if b.timezone:
        config["timezone"] = b.timezone
    return config


def fingerprint(b: BrowserConfig, state_dir: str | Path) -> dict[str, Any]:
    """The kept fingerprint, made first if there is none or its inputs changed,
    with browser.camoufox_config over it."""
    path = Path(state_dir) / FILE
    inputs = _inputs(b)
    try:
        kept = json.loads(path.read_text())
    except FileNotFoundError:
        kept = None
    except (OSError, ValueError) as e:
        log.warning("%s unreadable (%s): making the Camoufox fingerprint again", path, e)
        kept = None
    if kept is None or kept.get("inputs") != inputs:
        if kept is not None:
            log.warning("the config's screen, locale or timezone changed (%s -> %s): "
                        "making the Camoufox fingerprint again", kept.get("inputs"), inputs)
        kept = {"inputs": inputs, "config": _make(b)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(kept, indent=2, sort_keys=True) + "\n")
        log.info("Camoufox fingerprint written to %s", path)
    return {**kept["config"], **b.camoufox_config}


def env(config: dict[str, Any]) -> dict[str, str]:
    """The config as Camoufox's environment variables."""
    data = json.dumps(config, separators=(",", ":"), sort_keys=True)
    return {f"CAMOU_CONFIG_{i // CHUNK + 1}": data[i:i + CHUNK] for i in range(0, len(data), CHUNK)}
