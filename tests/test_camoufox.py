"""Camoufox's fingerprint: made once, kept across restarts, made again when
what it is made from changes."""

import json

from browser_mcp import camoufox
from browser_mcp.config import BrowserConfig


def test_it_is_kept_and_made_again_when_its_inputs_change(tmp_path, caplog):
    b = BrowserConfig(locale="en-AU", timezone="Australia/Sydney")
    first = camoufox.fingerprint(b, tmp_path)
    assert first == {"locale:language": "en", "locale:region": "AU", "timezone": "Australia/Sydney"}
    path = tmp_path / camoufox.FILE
    kept = path.read_text()
    assert json.loads(kept)["inputs"] == {"screen": [1920, 1080], "locale": "en-AU",
                                         "timezone": "Australia/Sydney"}

    # A restart: the same file, untouched.
    assert camoufox.fingerprint(b, tmp_path) == first
    assert path.read_text() == kept

    # The overrides merge over it at launch, and aren't written.
    b.camoufox_config = {"timezone": "Australia/Perth", "navigator.hardwareConcurrency": 8}
    assert camoufox.fingerprint(b, tmp_path) == {**first, "timezone": "Australia/Perth",
                                                 "navigator.hardwareConcurrency": 8}
    assert path.read_text() == kept

    # The screen changes: made again, and said so.
    b.camoufox_config = {}
    b.screen_width, b.screen_height = 2560, 1440
    with caplog.at_level("WARNING"):
        camoufox.fingerprint(b, tmp_path)
    assert "making the Camoufox fingerprint again" in caplog.text
    assert json.loads(path.read_text())["inputs"]["screen"] == [2560, 1440]


def test_a_locale_without_a_region_and_the_hosts_timezone(tmp_path):
    assert camoufox.fingerprint(BrowserConfig(locale="de"), tmp_path) == {"locale:language": "de"}


def test_the_environment_is_chunked_as_camoufox_reads_it():
    config = {"fonts": ["x" * 1000] * 70}
    env = camoufox.env(config)
    assert sorted(env) == ["CAMOU_CONFIG_1", "CAMOU_CONFIG_2", "CAMOU_CONFIG_3"]
    assert all(len(v) <= camoufox.CHUNK for v in env.values())
    assert json.loads("".join(env[f"CAMOU_CONFIG_{i}"] for i in (1, 2, 3))) == config
