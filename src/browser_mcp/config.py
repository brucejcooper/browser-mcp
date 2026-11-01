"""The configuration: defaults, then the TOML file, then the environment, then
the command line. Everything site- or deployment-specific lives here."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

# The URL deny list: a word here refuses a URL whose host or path has it as a
# whole word ("buy" refuses /buy/123 and buy.example.com, not /buying-guide).
DEFAULT_DENY_WORDS = [
    "login", "log-in", "logon", "signin", "sign-in", "signon", "sso", "oauth",
    "auth", "authorize", "register", "registration", "signup", "sign-up",
    "account", "accounts", "myaccount", "my-account",
    "password", "reset-password", "forgot-password",
    "cart", "basket", "checkout", "check-out", "payment", "payments",
    "pay", "purchase", "bid", "bids", "bidding", "buy",
    "buynow", "buy-now", "offer", "offers", "makeoffer", "make-offer",
    "watchlist", "subscribe", "unsubscribe", "logout", "signout", "sign-out",
]

# Loopback, RFC 1918, CGNAT, link-local, ULA, and the other non-public ranges.
DEFAULT_DENY_NETWORKS = [
    "0.0.0.0/8", "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
    "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "192.0.0.0/24",
    "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "::ffff:0:0/96", "fc00::/7", "fe80::/10", "ff00::/8",
]


@dataclass
class Engine:
    url: str
    input: str = 'input[name="q"]'
    result: str = ""
    title: str = "h2, h3"
    link: str = "a[href]"
    snippet: str = ""


@dataclass
class Site:
    url: str
    input: str = ""  # empty: the page's first search box


DEFAULT_ENGINES = {
    "duckduckgo": Engine(
        url="https://duckduckgo.com/",
        input='input[name="q"], textarea[name="q"], #searchbox_input',
        result='article[data-testid="result"]',
        title="h2",
        link='a[data-testid="result-title-a"]',
        snippet='[data-result="snippet"]',
    ),
    "bing": Engine(
        url="https://www.bing.com/",
        input='textarea[name="q"], input[name="q"]',
        result="li.b_algo",
        title="h2",
        link="h2 a",
        snippet=".b_caption p, p",
    ),
    "google": Engine(
        url="https://www.google.com/",
        input='textarea[name="q"], input[name="q"]',
        result="#search div.MjjYud, #search div.g",
        title="h3",
        link="a:has(h3)",
        snippet='div[data-sncf], div.VwiC3b, div[style*="-webkit-line-clamp"]',
    ),
    "brave": Engine(
        url="https://search.brave.com/",
        input='input[name="q"], textarea[name="q"]',
        result='div.snippet[data-type="web"]',
        title=".title, .search-snippet-title",
        link="a[href]",
        snippet=".snippet-description, .generic-snippet .content",
    ),
}


@dataclass
class BrowserConfig:
    locale: str = "en-US"
    timezone: str = ""  # empty: the host's
    screen_width: int = 1920
    screen_height: int = 1080
    idle_timeout: float = 600.0  # seconds without a call before the browser closes
    headless: bool = False  # for tests only: headless Firefox is easier to spot
    display: str = ""  # an existing X display to use instead of our own Xvfb
    navigation_timeout: float = 45.0
    challenge_wait: float = 25.0  # how long to wait out an interstitial
    checkbox_wait: float = 15.0  # how long a clicked checkbox has to clear
    firefox_prefs: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutputConfig:
    max_text: int = 30_000
    max_links: int = 200
    max_results: int = 20


@dataclass
class LimitsConfig:
    delay_min: float = 2.0  # seconds between page actions, jittered
    delay_max: float = 3.0
    per_site_per_day: int = 250
    per_day: int = 1000
    per_caller_per_hour: int = 50


@dataclass
class DenyConfig:
    words: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_WORDS))
    patterns: list[str] = field(default_factory=list)  # regexes on the whole URL
    networks: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_NETWORKS))
    # A host that doesn't resolve here can't be checked. True lets it through
    # an upstream proxy (which resolves it); direct connections always refuse.
    unresolvable_via_proxy: bool = False


@dataclass
class ProxyConfig:
    url: str = ""  # empty: HTTPS_PROXY / HTTP_PROXY from the environment
    no_proxy: str = ""  # empty: NO_PROXY from the environment
    username: str = ""
    password_file: str = ""


@dataclass
class TlsConfig:
    ca_files: list[str] = field(default_factory=list)


@dataclass
class LogConfig:
    file: str = ""  # empty: stderr


@dataclass
class MetricsConfig:
    enabled: bool = False  # /metrics on the HTTP port
    textfile: str = ""  # or a node_exporter textfile, rewritten after each page


@dataclass
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/mcp"
    token_file: str = ""  # required for any bind but loopback
    caller_header: str = "X-Caller-Id"


@dataclass
class StdioConfig:
    caller: str = "stdio"


@dataclass
class SearchConfig:
    default: str = "duckduckgo"


@dataclass
class Config:
    state_dir: str = "./state"
    transport: str = "stdio"
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    deny: DenyConfig = field(default_factory=DenyConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    tls: TlsConfig = field(default_factory=TlsConfig)
    log: LogConfig = field(default_factory=LogConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    stdio: StdioConfig = field(default_factory=StdioConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    engines: dict[str, Engine] = field(default_factory=lambda: dict(DEFAULT_ENGINES))
    sites: dict[str, Site] = field(default_factory=dict)


class ConfigError(ValueError):
    pass


def _merge(obj: Any, data: dict[str, Any], where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"{where}{key}: unknown setting")
        current = getattr(obj, key)
        if key in ("engines", "sites") and obj.__class__ is Config:
            cls = Engine if key == "engines" else Site
            table = dict(current)
            for name, entry in value.items():
                try:
                    table[name] = cls(**entry)
                except TypeError as e:
                    raise ConfigError(f"{key}.{name}: {e}") from None
            setattr(obj, key, table)
        elif is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"{where}{key}: expected a table")
            _merge(current, value, f"{where}{key}.")
        else:
            if isinstance(current, bool) != isinstance(value, bool) or (
                isinstance(current, (int, float)) and not isinstance(value, (int, float))
            ) or (isinstance(current, (str, list, dict)) and type(value) is not type(current)):
                raise ConfigError(f"{where}{key}: expected {type(current).__name__}")
            setattr(obj, key, float(value) if isinstance(current, float) else value)


def load(path: str | os.PathLike | None, env: dict[str, str] | None = None) -> Config:
    """The file's settings over the defaults, then the environment's."""
    env = os.environ if env is None else env
    cfg = Config()
    if path:
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f), "")
    if env.get("BROWSER_MCP_STATE_DIR"):
        cfg.state_dir = env["BROWSER_MCP_STATE_DIR"]
    if env.get("BROWSER_MCP_CA_FILES"):
        cfg.tls.ca_files += [p for p in env["BROWSER_MCP_CA_FILES"].split(os.pathsep) if p]
    if env.get("BROWSER_MCP_TOKEN_FILE"):
        cfg.http.token_file = env["BROWSER_MCP_TOKEN_FILE"]
    if env.get("BROWSER_MCP_LOG_FILE"):
        cfg.log.file = env["BROWSER_MCP_LOG_FILE"]
    if env.get("BROWSER_MCP_PROXY_USER"):
        cfg.proxy.username = env["BROWSER_MCP_PROXY_USER"]
    if env.get("BROWSER_MCP_PROXY_PASSWORD_FILE"):
        cfg.proxy.password_file = env["BROWSER_MCP_PROXY_PASSWORD_FILE"]
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    lim = cfg.limits
    if not 0 <= lim.delay_min <= lim.delay_max:
        raise ConfigError("limits: delay_min must be between 0 and delay_max")
    for name in ("per_site_per_day", "per_day", "per_caller_per_hour"):
        if getattr(lim, name) < 0:
            raise ConfigError(f"limits.{name}: must not be negative")
    if cfg.search.default not in cfg.engines:
        raise ConfigError(f"search.default: no engine {cfg.search.default!r}")
    if cfg.transport not in ("stdio", "http"):
        raise ConfigError("transport: stdio or http")
    for p in cfg.tls.ca_files:
        if not Path(p).is_file():
            raise ConfigError(f"tls.ca_files: {p} is not a file")
