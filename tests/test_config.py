import pytest

from browser_mcp.config import ConfigError, load


def test_defaults():
    cfg = load(None, env={})
    assert cfg.limits.per_site_per_day == 250
    assert cfg.limits.per_day == 1000
    assert cfg.limits.per_caller_per_hour == 50
    assert (cfg.limits.delay_min, cfg.limits.delay_max) == (2.0, 3.0)
    assert cfg.limits.cool_off == 0
    assert cfg.http.host == "127.0.0.1"
    assert cfg.search.default == "duckduckgo"
    assert "127.0.0.0/8" in cfg.deny.networks and "fc00::/7" in cfg.deny.networks


def test_example_config_loads(tmp_path):
    from pathlib import Path
    cfg = load(Path(__file__).parent.parent / "examples" / "config.toml", env={})
    assert cfg.sites


def test_file_env_and_unknown_keys(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    f = tmp_path / "c.toml"
    f.write_text(f'''
state_dir = "/tmp/s"
[limits]
per_day = 10
[tls]
ca_files = ["{ca}"]
[sites.example]
url = "https://www.example.com/"
input = "#q"
''')
    cfg = load(f, env={"BROWSER_MCP_CA_FILES": str(ca), "BROWSER_MCP_STATE_DIR": "/tmp/t"})
    assert cfg.limits.per_day == 10
    assert cfg.tls.ca_files == [str(ca), str(ca)]
    assert cfg.state_dir == "/tmp/t"
    assert cfg.sites["example"].input == "#q"
    f.write_text("[limits]\nper_dya = 1\n")
    with pytest.raises(ConfigError, match="per_dya"):
        load(f, env={})
    f.write_text("[limits]\ncool_off = -1\n")
    with pytest.raises(ConfigError, match="limits.cool_off: must not be negative"):
        load(f, env={})
    f.write_text('[limits]\nper_day = "many"\n')
    with pytest.raises(ConfigError, match="expected int"):
        load(f, env={})


def test_the_engine_is_firefox_or_camoufox(tmp_path):
    assert load(None, env={}).browser.engine == "firefox"
    f = tmp_path / "c.toml"
    f.write_text('[browser]\nengine = "camoufox"\n')
    with pytest.raises(ConfigError, match="camoufox needs its executable"):
        load(f, env={})
    assert load(f, env={"BROWSER_MCP_CAMOUFOX": "/x/camoufox-bin"}).browser.camoufox_path == "/x/camoufox-bin"
    f.write_text('[browser]\nengine = "chrome"\n')
    with pytest.raises(ConfigError, match="firefox or camoufox"):
        load(f, env={})
