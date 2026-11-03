# browser-mcp

An [MCP](https://modelcontextprotocol.io) server that browses and searches the
web on its user's behalf, in a real Firefox that sites don't turn away as a
bot. It is for occasional lookups (a price, a listing, whether something is in
stock), a few pages at a time.

**What it isn't:**
- **Not a crawler.** Every page counts against limits per site, per day and
  per caller, and the pages are paced.
- **Not a way to act on sites.** It has no general click, type or form tool,
  so no logins, accounts, bids or purchases, and a URL deny list refuses those
  pages outright. The one tool that clicks, `challenge`, acts only inside a
  bot challenge's widget, and only while a page is stopped at one.
- **Not a CAPTCHA-solving service.** It waits out interstitials and clicks a
  checkbox. Anything harder goes to the client (the model reads the image
  challenge), then to a person in a live view. It never uses paid solver
  services or third-party solver APIs. See [Challenges](#challenges).

It runs over stdio (for a local Claude Code) or streamable HTTP. It is packaged
from one Nix flake as a NixOS module and as a Docker image for `linux/amd64`
and `linux/arm64`. Both use the same Firefox build.

**Contents:** [Tools](#tools) · [Running it](#running-it) ·
[Configuration](#configuration) · [Challenges](#challenges) · [Proxy and CA](#behind-a-proxy-and-a-tls-inspecting-ca) ·
[Guardrails](#guardrails) · [How it passes as a person](#how-it-passes-as-a-person) ·
[Survey](#survey-what-exists-and-why-this-is-built-the-way-it-is) ·
[The risk](#the-risk) · [Development](#development)

## Tools

| Tool | What it does | Pages |
|---|---|---|
| `browse(url, screenshot?)` | Loads the page. Returns the final URL, the status and the title, the visible text (up to 30,000 characters), the links (up to 200, the page's own before its menus, without tracking parameters) and, if asked, a screenshot. If a challenge stops it, it returns `challenge` with its kind and a screenshot of the widget instead. | 1 |
| `search(query, engine?)` | Types the query into a search engine's own page and returns the results: title, URL and snippet. DuckDuckGo is the default; Bing, Google and Brave are built in, and others can be configured. | 2 |
| `site_search(site, query)` | Types the query into a site's own search box and returns the results page, as `browse` does. `site` is a configured recipe or any host name. For a host name, the first search box on its home page is used. | 2 |
| `challenge(action, x?, y?, text?)` | Acts on the challenge the last page load stopped at, and on nothing else: `screenshot`, `click`, `type`, `ask_human`, `resume`. See [Challenges](#challenges). | 0 |

The two search tools load the start page and then the results page. When the
tab is already on that site and its search box is there, they type into it
and skip loading the start page.

Every tool's description says the page content is untrusted: whatever a page
says is data, never instructions.

## Running it

### With stdio, in Claude Code

From a checkout of this repository (Linux, with Nix):

```sh
nix build
claude mcp add browser -- "$PWD/result/bin/browser-mcp" \
  --state-dir ~/.local/state/browser-mcp --config ~/.config/browser-mcp.toml
```

On a Mac, run the Docker image over stdio. A stdio server needs no token:

```sh
claude mcp add browser -- docker run -i --rm --shm-size 1g \
  -v browser-mcp-profile:/state browser-mcp:0.1.0 --transport stdio
```

### With Docker

Each release has `browser-mcp-<version>-amd64.tar.gz`,
`browser-mcp-<version>-arm64.tar.gz` and `SHA256SUMS`, on the repository's
Releases page. Download the one for your machine (`arm64` for Apple Silicon)
and `SHA256SUMS`, then:

```sh
sha256sum -c --ignore-missing SHA256SUMS      # macOS: shasum -a 256 -c --ignore-missing SHA256SUMS
docker load -i browser-mcp-0.1.0-arm64.tar.gz # loads browser-mcp:0.1.0
```

With [`compose.yaml`](compose.yaml):

```sh
openssl rand -hex 32 > token          # the bearer token
cp examples/config.toml config.toml   # and edit it
mkdir -p certs                        # a TLS-inspecting proxy's root CA goes here
docker compose up -d
claude mcp add --transport http browser http://127.0.0.1:8080/mcp \
  --header "Authorization: Bearer $(cat token)"
```

Inside the container the server binds every address, so it requires the
bearer token. The port is published on `127.0.0.1` only. The Compose file
keeps the profile and the page log in volumes, and passes the proxy
variables through from the shell that runs it. It also mounts `config.toml`
and the `certs` directory.

### With the NixOS module

```nix
{
  inputs.browser-mcp.url = "<this repository>";
  outputs = { nixpkgs, browser-mcp, ... }: {
    nixosConfigurations.example = nixpkgs.lib.nixosSystem {
      modules = [
        browser-mcp.nixosModules.default
        {
          services.browser-mcp = {
            enable = true;
            settings = {
              http.host = "0.0.0.0";
              browser = { locale = "en-GB"; timezone = "Europe/London"; };
              sites.example = { url = "https://shop.example.com/"; input = "#search"; };
            };
            tokenFile = "/run/secrets/browser-mcp-token"; # required off loopback
            openFirewall = true;
          };
        }
      ];
    };
  };
}
```

`settings` is the config file below, written as Nix. The unit runs as its own
`browser-mcp` user, with `StateDirectory`, `ProtectSystem=strict`,
`MemoryMax` (`memoryMax`, default 3G) and the rest of systemd's hardening. The
server starts its own Xvfb. The token and the proxy password
(`proxyPasswordFile`) reach it as systemd credentials. `environment` sets
extra variables, such as `HTTPS_PROXY`.

## Configuration

The settings come from a TOML file (`--config`, or `BROWSER_MCP_CONFIG`),
then the environment, then the command line. Every setting is optional.
[`examples/config.toml`](examples/config.toml) lists them all with their
defaults. Unknown settings are an error, so a typo doesn't pass silently.

| Setting | Default | What it is |
|---|---|---|
| `state_dir` | `./state` | The browser profile (cookies, history) and the limits' counters. `--state-dir`, `BROWSER_MCP_STATE_DIR`. |
| `transport` | `stdio` | `stdio` or `http`. `--transport`. |
| `browser.locale` | `en-US` | The browser's locale and Accept-Language. |
| `browser.timezone` | the host's | An IANA zone, e.g. `Europe/Berlin`. |
| `browser.screen_width`, `screen_height` | 1920, 1080 | The virtual screen, and the window's size. |
| `browser.idle_timeout` | 600 | Seconds without a call before the browser closes. The profile is kept. |
| `browser.display` | (own Xvfb) | An X display to use instead of the server's own Xvfb. |
| `browser.navigation_timeout` | 45 | Seconds a page may take to load. |
| `browser.challenge_wait`, `checkbox_wait` | 25, 15 | Seconds to wait out an interstitial, and for a clicked checkbox to clear. |
| `browser.firefox_prefs` | `{}` | Extra Firefox prefs. |
| `output.max_text`, `max_links`, `max_results` | 30000, 200, 20 | The caps on what a tool returns. |
| `limits.delay_min`, `delay_max` | 2.0, 3.0 | Seconds between page actions, jittered. |
| `limits.per_site_per_day` | 250 | Pages per registrable domain (`example.co.uk`) in any 24 hours. |
| `limits.per_day` | 1000 | Pages in all in any 24 hours. |
| `limits.per_caller_per_hour` | 50 | Pages per caller in any hour. |
| `deny.words` | login, signin, register, account, cart, checkout, bid, buy, offer, password, payment, … | A URL whose host or path has one of these as a whole word is refused. `/buy/1` is refused; `/buying-guide` is not. The query string isn't checked, so a search for "buy" is fine. |
| `deny.patterns` | `[]` | Regexes on the whole URL, also refused. |
| `deny.networks` | loopback, RFC 1918, CGNAT, link-local, ULA, multicast, … | Addresses the browser may never reach. |
| `deny.unresolvable_via_proxy` | false | A host that doesn't resolve here can't be checked. If true, it may still go through an upstream proxy, which resolves it. |
| `proxy.url`, `no_proxy` | the environment's | See [the proxy](#the-proxy). |
| `proxy.username`, `password_file` | | Basic proxy auth. `BROWSER_MCP_PROXY_USER`, `BROWSER_MCP_PROXY_PASSWORD_FILE`. |
| `tls.ca_files` | `[]` | Extra CA files to trust. `--ca-file` (repeatable), `BROWSER_MCP_CA_FILES` (colon-separated). All three add up. |
| `log.file` | stderr | The page log. `--log-file`, `BROWSER_MCP_LOG_FILE`. |
| `metrics.enabled` | false | Prometheus counters at `/metrics` on the HTTP port, behind the same bearer. |
| `metrics.textfile` | | Or a node_exporter textfile, rewritten after each page. |
| `http.host`, `port`, `path` | `127.0.0.1`, 8080, `/mcp` | Where streamable HTTP listens. `--host`, `--port`. |
| `http.token_file` | | The bearer token, at least 16 characters. Any bind but loopback refuses to start without it. `--token-file`, `BROWSER_MCP_TOKEN_FILE`. |
| `http.caller_header` | `X-Caller-Id` | The request header naming the caller, for the per-caller limit and the log. |
| `stdio.caller` | `stdio` | The caller over stdio. `--caller`. |
| `view.mode` | `local` | The live view's mode: `local` or `proxied`. See [the live view](#2-a-person-in-the-live-view). |
| `view.host`, `port` | `127.0.0.1`, 0 | Where the live view listens. `local` always binds `127.0.0.1`; port 0 is any free port. `proxied` needs a fixed port. |
| `view.public_url` | | `proxied`: the proxy's URL for the view, the base of the link handed out. It must be the root of its own host (`https://view.example.org/`), not a path under another site. |
| `view.user_header` | | `proxied`: the request header the proxy names the authenticated user in, e.g. `X-authentik-username`. A request without it is refused. |
| `view.timeout` | 600 | Seconds before an unfinished hand-off closes and fails. |
| `view.novnc_dir` | `BROWSER_MCP_NOVNC_DIR` | noVNC's files (the directory with `core/rfb.js`). The Nix package and the image set it. |
| `search.default` | `duckduckgo` | The engine `search` uses when none is named. |
| `engines.<name>` | duckduckgo, bing, google, brave | `url` (the start page), `input` (the search box), and `result`, `title`, `link` and `snippet` (CSS selectors for the results). If `result` matches nothing, links holding a heading are taken instead. |
| `sites.<name>` | | `url` (the start page) and `input` (the search box; empty means the first search box). |

The example config's sites are neutral (Wikipedia, the Internet Archive). A
deployment adds its own.

## Challenges

When a page stops at a bot challenge, the server first tries the trivial ways
past it: it waits out an interstitial, and clicks a checkbox widget (see
[How it passes as a person](#how-it-passes-as-a-person)). A widget whose
response token gets filled in counts as passed. What's left comes back from
`browse`, `search` or `site_search` as `challenge`, with its kind and a
screenshot, and there are two ways on, in this order.

### 1. The client answers it

The model reads the image challenge and acts, with `challenge(action)`:

| Action | What it does |
|---|---|
| `screenshot` | The widget: the largest visible frame of the challenge's kind, so an image challenge's frame once it has opened, else the checkbox's. With the frame's box. |
| `click {x, y}` | Clicks at `x`, `y` relative to the frame's top left, in the screenshot's pixels, along a human-like mouse path. A click outside the frame's box is refused. Returns whether the challenge is still there, with a new screenshot. |
| `type {text}` | Types (up to 100 characters, with a human rhythm) into the text input that has the focus inside the frame; click it first. Anywhere else is refused. |
| `ask_human` | Opens the live view (below) and returns its link and expiry. Once per challenge. |
| `resume` | Once the challenge is gone, returns the page as `browse` does. While a person is still at it, it says so. |

It is held to the challenge so it can't become a general click tool:
- every action is refused unless the last page load stopped at a challenge
  that is still pending; the next page load ends it;
- only the caller that met the challenge may act on it;
- interstitials and block pages have no widget, so only `ask_human` and
  `resume` work on them;
- every action, refusals too, is a line in the page log (`tool: challenge`,
  with the `action` and its `outcome`; what was typed is not logged, only its
  length), and is counted in `browser_mcp_challenge_actions`.

Clicks and typing don't count against the page limits; a page the challenge
lets through was counted when it was asked for.

### 2. A person, in the live view

`ask_human` opens a short-lived live view of the browser's own display: x11vnc
on the server's Xvfb, which shows only this browser, and noVNC's page in front
of it, with a Done button. Nothing of it runs until `ask_human`, and nothing
is left listening after:
- x11vnc listens on a Unix socket in a private directory, with no TCP port at
  all (`-rfbport 0 -rfbportv6 -1`);
- the view's own web server bridges noVNC's WebSocket to that socket, and
  stops with the view.

**It closes itself**, and the hand-off ends, when the person presses Done,
when the challenge detector sees the challenge clear, or when `view.timeout`
(default 10 minutes) passes. Meanwhile the other tools are refused: the
browser is the person's.

**The link** carries a random token, good for this challenge only and for one
session. The first open takes it and sets a cookie (`HttpOnly`,
`SameSite=Strict`); everything else needs that cookie. A second open, from
anywhere else, is refused, as is another challenge's token.

**The modes** (`view.mode`):
- `local` (the default) binds `127.0.0.1`, on any free port, and checks the
  token. It is for a laptop: the link goes back to the client, and Claude Code
  shows it to the user. From inside the Docker image, `127.0.0.1` is the
  container's own, so the link isn't reachable from the host there: use the
  Nix package, or `proxied`.
- `proxied` binds `view.host` and `view.port`, and expects an authenticating
  reverse proxy in front, which the deployment provides. Every request needs
  the header the proxy names its user in (`view.user_header`), and the token
  as well; the first user to open the link is the only one who may use it. The
  link is built on `view.public_url`. Bind it where only the proxy can reach
  it: the header is trusted as it comes.

**If it isn't solved**, the challenge is reported as failed, and the site is
left alone for the rest of the day: until local midnight, every page on it is
refused, across restarts. That happens when the view times out, or when the
person presses Done with the challenge still there.

**Never:** paid solver services, or third-party solver accounts or APIs.
Audio challenges aren't handled.

**The risk.** Solving a challenge is the clearest case of getting past a bot
check, and what's at stake is the same as in [The risk](#the-risk): the
reputation of the IP address the browser goes out from. A live view is a
remote control of the browser for whoever holds the link: hand it only to the
user, keep `proxied` behind the proxy, and keep the timeout short.

## Behind a proxy and a TLS-inspecting CA

### The proxy

`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY` (either case) are
honoured, or `proxy.url` and `proxy.no_proxy` in the config. `NO_PROXY` is
read as curl reads it: `example.com` covers its subdomains, and IPs, CIDRs,
`host:port` and `*` all work. Proxy auth is basic: a user and password in the
proxy URL, or `proxy.username` with `proxy.password_file` (or the
`BROWSER_MCP_PROXY_*` variables). The password is read from a file and never
put in the config.

**An NTLM- or Kerberos-only proxy** (common in companies) needs a local
[px](https://github.com/genotrance/px) or [cntlm](https://cntlm.sourceforge.net/)
in front, which does the Windows auth and offers a plain proxy on localhost.
Point `HTTPS_PROXY` at that. From inside Docker, the host's localhost is
`host.docker.internal`.

### The extra CA

A proxy that inspects TLS re-signs every site with its own root. Give that
root to the server by `--ca-file`, `BROWSER_MCP_CA_FILES` or `tls.ca_files`.
PEM (a bundle is fine) and DER both work.
- **The browser** trusts it because it is imported into the profile's NSS
  store with `certutil` on each start. A CA no longer configured is removed
  again.
- **The server's own connections** trust it too. The only one is to an
  `https://` proxy.

Without the CA, a page fails with an error that says so, e.g.
`certificate error (SEC_ERROR_UNKNOWN_ISSUER): the site's certificate isn't
trusted. Behind a TLS-inspecting proxy, give its CA with --ca-file, …`.

`tests/test_proxy_ca.py` tests all of this behind `mitmproxy` and its root:
the CA given each way, a clear error without it, the proxy used, and
`NO_PROXY` bypassing it.

## Guardrails

All of these are in code, with their values in config.

- **Egress.** The browser sends every request through a small forward proxy
  inside the server, on loopback. That covers each navigation, each redirect
  hop and each image, script and XHR, and also the localhost requests Firefox
  would otherwise send direct. For each request, the proxy resolves the host
  and refuses it if any of its addresses is in `deny.networks`, before
  anything connects. It then connects to the address it checked, so a second,
  different DNS answer can't slip past (DNS rebinding). From there a request
  goes direct, or through the upstream proxy. The tools check the first URL
  the same way before loading it, and a redirect into a denied network fails
  the call with a message saying so. WebRTC is held to the proxy, and DNS over
  HTTPS is off, so neither gets around the check.
- **The URL deny list**, checked before loading and again on where the page
  ended up.
- **The limits.** A page is a top-level navigation. The caller comes from
  `http.caller_header` over HTTP, or `stdio.caller` over stdio. Over a limit,
  the call is refused with when it frees up, e.g. `over the limit of 250
  pages a day on example.com: refused; it frees up at 2026-11-02T03:04:05+00:00
  (in 5025 s)`. The counts survive restarts.
- **The page log**: one JSON line per page, with `time`, `caller`, `tool`,
  `url`, `final_url`, `status`, `bytes`, `challenge` and `outcome` (`ok`,
  `challenge`, `refused:<why>`, `certificate_error`, `error`).
- **The HTTP transport** binds `127.0.0.1` by default. On any other bind it
  refuses to start without a bearer token, and refuses every request without
  it (401).

## How it passes as a person

- **The engine.** Playwright drives Firefox (Playwright's build, from nixpkgs
  `playwright-driver.browsers`). Firefox runs headful under an Xvfb of its own,
  with one persistent profile, so cookies and history carry over between
  calls.
  - The screen is 1920×1080, and the window fills it.
  - The fonts are real: DejaVu, Liberation and Noto Color Emoji.
  - The locale and time zone come from config.
  - There is one tab, and calls are serialised.
  - Popups are closed as they open.
- **`navigator.webdriver` reads `false`**, as in a person's Firefox, from a
  getter that still looks native. Firefox's `dom.webdriver.enabled` pref
  doesn't change it.
- **Human-like input** (`human.py`):
  - the mouse moves along curved Bézier paths, eased, with a little tremor
    and the odd overshoot;
  - a few idle movements and a wheel scroll after each page loads, and now
    and then a page down by clicking the scroll bar's track;
  - typing has a jittered rhythm and pauses between words;
  - after `load`, a dwell of a second or so.
- **The referer** is kept when following a link from the page last loaded.
- **Trivial challenges.** Cloudflare (`cf-mitigated`, "Just a moment..."),
  Turnstile, hCaptcha, reCAPTCHA and the Akamai, PerimeterX and DataDome block
  pages are detected.
  - Interstitials are waited out.
  - A checkbox widget is clicked along a human-like path.
  - Anything else comes back as `challenge` (its kind, and a screenshot of
    the widget), for the client or a person to solve (see
    [Challenges](#challenges)).
  - A widget on an ordinary page (reCAPTCHA v3, a form's hCaptcha) isn't
    treated as a challenge.

## Survey: what exists, and why this is built the way it is

Done 2026-11-01. The rule was to prefer maintained work over our own code.
The baseline came from a spike that day, from a residential connection:
Playwright (Python) with Firefox, headful under Xvfb, with a persistent
profile. It passed eBay (Akamai Bot Manager) and two shops behind Cloudflare's
managed challenge. Playwright with Chromium, its usual tells removed, was
refused by eBay with a 403 (no `_abck` cookie).

| Project | What it is | Verdict |
|---|---|---|
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) (`@playwright/mcp`) | Microsoft's MCP server over Playwright: navigate, click, type, fill forms, by the accessibility tree. | **Not used.** Its tools are exactly the general click, type and form tools this server must not have. It has no limits, egress control or challenge handling, and adding them means wrapping or forking it. We use the same engine (Playwright) underneath. |
| [browser-use](https://github.com/browser-use/browser-use) | An agent framework: an LLM drives a Chromium browser step by step. | **Not used.** It is an agent with its own LLM loop, not a tool. It is Chromium-based, which eBay refused in the spike, and it is built to act on pages, which this is not for. |
| [Camoufox](https://github.com/daijro/camoufox) | A Firefox with fingerprint patches in C++, driven through Playwright's API. MPL-2.0 / MIT. | **The fallback engine.** Active, but it calls itself "under development". Its binary is fetched at run time (`camoufox fetch`) and isn't in nixpkgs. Today's Firefox passes, so it isn't needed yet. If Firefox starts being caught, it slots in where the engine is launched (`browser.py`), packaged as a fixed-output fetch. |
| [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright), [nodriver](https://github.com/ultrafunkamsterdam/nodriver) | Undetected drivers for Chromium (a patched Playwright; CDP without WebDriver). | **Not used.** They are Chromium only. The spike's evidence favours Firefox against Akamai. |
| [invisible-playwright-mcp](https://github.com/ColorSource/invisible_playwright_mcp) | An MCP server over a patched stealth Firefox, mirroring Playwright MCP's tools. | **Not used.** It has the same general click and type tool surface. It pins its own engine binary, outside nixpkgs. |
| Human-input libraries: [ghost-cursor](https://github.com/Xetera/ghost-cursor) (JS), [python_ghost_cursor](https://github.com/mcolella14/python_ghost_cursor), [humancursor-playwright](https://pypi.org/project/humancursor-playwright/), smooth-cursor-playwright, OxyMouse | Bézier or Gaussian mouse paths, some with scrolling and clicking. | **Not used; the idea is borrowed.** ghost-cursor is for Puppeteer in JS. Its Python port was last released in 2021. The Playwright ports are single-author 0.x packages, not in nixpkgs. A dependency each for about 100 lines (`human.py`) isn't worth it, and none of them do typing rhythm or scroll-bar paging. |

**The choice:** Playwright (Python) with nixpkgs Firefox, as in the spike, and
our own thin layer: the tools, the limits, the egress proxy, human input and
challenges. Python because the MCP SDK and Playwright both have maintained
Python packages in nixpkgs. Firefox from nixpkgs, so the NixOS service and the
Docker image share one build and one fingerprint.

## The risk

Getting past bot checks may breach a site's terms of service. What is at stake
is **the reputation of the IP address the browser goes out from**, with the
sites and with their bot-management vendors (Akamai, Cloudflare and others).
If it is flagged, the person behind that IP can be challenged or blocked in
their own browser too.
- The limits keep the volume low, but bot managers react to bursts as much as
  to totals.
- **Behind an employer's proxy, that is the employer's egress**, and its
  acceptable-use policy applies.

Use it for the occasional lookup it is meant for.

## Development

```sh
nix develop                                  # Python, Firefox, Xvfb, certutil, mitmproxy
python -m pytest                             # everything but the live tests
nix build .#default .#image                  # the package (with its unit tests) and the image
nix build .#checks.x86_64-linux.module       # the NixOS module evaluates and its unit builds
```

- `tests/test_browser.py` drives the real Firefox against local pages.
- `tests/test_challenge.py` solves a stand-in widget through `challenge`, and
  hands it to a person in the live view (the real x11vnc, and noVNC in a
  second Firefox).
- `tests/test_proxy_ca.py` runs behind mitmproxy.

CI ([`.forgejo/workflows/ci.yml`](.forgejo/workflows/ci.yml)) runs the tests
and gitleaks, and builds the package and both images. When `main`'s version
(in `pyproject.toml`) has no release yet, it makes one
([`scripts/release.sh`](scripts/release.sh)): a release `v<version>` with
both images' tarballs and `SHA256SUMS`, using the job's own token.

To release, bump `version` in `pyproject.toml`.

## Licence

MIT. See [LICENSE](LICENSE).
