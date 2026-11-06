"""The tools against local pages in a real Firefox under Xvfb."""

import json
import os
import shutil

import pytest

from browser_mcp.config import Config, Engine, Site
from browser_mcp.server import build
from browser_mcp.tools import ToolRefused

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not (shutil.which("Xvfb") and shutil.which("certutil")), reason="needs Xvfb and certutil"),
]

DENIED = ["127.0.0.2/32"]  # the fixtures' "private" server; 127.0.0.1 stands in for the internet

# The engine the browser tests run in: firefox, or camoufox (CI runs them in each).
ENGINE = os.environ.get("BROWSER_MCP_TEST_ENGINE", "firefox")


def with_engine(cfg: Config) -> Config:
    cfg.browser.engine = ENGINE
    cfg.browser.camoufox_path = os.environ.get("BROWSER_MCP_CAMOUFOX", "")
    return cfg


def config(tmp_path, **limits) -> Config:
    cfg = with_engine(Config(state_dir=str(tmp_path / "state")))
    cfg.deny.networks = list(DENIED)
    cfg.limits.delay_min = cfg.limits.delay_max = 0.0
    for k, v in limits.items():
        setattr(cfg.limits, k, v)
    cfg.browser.challenge_wait = 6
    cfg.browser.checkbox_wait = 3
    cfg.log.file = str(tmp_path / "pages.log")
    return cfg


@pytest.fixture
async def service(tmp_path):
    services = []

    async def make(cfg=None):
        _, svc = build(cfg or config(tmp_path))
        services.append(svc)
        return svc

    yield make
    for svc in services:
        await svc.browser.close()
        await svc.egress.stop()


PAGE = """<!doctype html><title>Fixture</title><body>
<h1>Hello there</h1><p>Price: $123.45, 7 in stock.</p>
<a href="/next">Next page</a> <a href="/next">Next page</a>
<div style="height:3000px"></div></body>"""


async def test_browse_returns_text_links_and_a_human_looking_browser(service, site, tmp_path):
    site.pages["/"] = PAGE
    site.pages["/next"] = "<title>Next</title><p>second</p>"
    svc = await service()
    r = await svc.browse(site.base + "/", "t", screenshot=True)
    assert r.status == 200 and r.title == "Fixture"
    assert "Price: $123.45, 7 in stock." in r.text
    assert r.links == [{"text": "Next page", "href": site.base + "/next"}]
    assert r.screenshot and r.challenge is None
    page = await svc.browser.page()
    env = await page.evaluate("""() => ({wd: navigator.webdriver, sw: screen.width, sh: screen.height,
        getter: Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver').get.toString()})""")
    assert env["wd"] is False
    assert env["getter"] == "function webdriver() {\n    [native code]\n}"
    assert (env["sw"], env["sh"]) == (1920, 1080)

    # Following a link from the page keeps it as the referer.
    r = await svc.browse(site.base + "/next", "t")
    assert r.text == "second"
    assert site.requests[-1][1].get("Referer") == site.base + "/"
    lines = [json.loads(l) for l in (tmp_path / "pages.log").read_text().splitlines()]
    assert [l["outcome"] for l in lines] == ["ok", "ok"]
    assert lines[0]["caller"] == "t" and lines[0]["status"] == 200 and lines[0]["bytes"] > 0


FINGERPRINT = """() => {
  const native = f => Function.prototype.toString.call(f);
  const getter = name => Object.getOwnPropertyDescriptor(Navigator.prototype, name).get;
  const gl = document.createElement('canvas').getContext('webgl');
  const gl2 = document.createElement('canvas').getContext('webgl2');
  const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
  let wrongThis = null;
  try { getter('webdriver').call({}); } catch (e) { wrongThis = e.constructor.name; }
  return {
    pdfViewerEnabled: navigator.pdfViewerEnabled, plugins: navigator.plugins.length,
    mimeTypes: navigator.mimeTypes.length,
    webgl: !!gl, webgl2: !!gl2, renderer: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null,
    languages: navigator.languages, language: navigator.language,
    intl: Intl.DateTimeFormat().resolvedOptions().locale,
    webdriver: navigator.webdriver, getter: native(getter('webdriver')),
    getterName: getter('webdriver').name, wrongThis,
    toString: native(Function.prototype.toString), toStringName: Function.prototype.toString.name,
    own: native(function own() { return 1; }),
  };
}"""


async def test_the_fingerprint_is_a_persons_firefox(service, site, tmp_path):
    """The tells #7 measured: the PDF viewer, WebGL, the webdriver getter and
    the languages, as a person's Firefox has them."""
    site.pages["/"] = PAGE
    cfg = config(tmp_path)
    cfg.browser.locale = "en-AU"
    svc = await service(cfg)
    await svc.browse(site.base + "/", "t")
    env = await (await svc.browser.page()).evaluate(FINGERPRINT)
    assert (env["pdfViewerEnabled"], env["plugins"], env["mimeTypes"]) == (True, 5, 2)
    assert env["webgl"] and env["webgl2"] and env["renderer"]
    assert env["languages"] == ["en-AU", "en"] and env["language"] == "en-AU"
    assert env["intl"] == "en-AU"
    assert site.requests[-1][1]["Accept-Language"] == "en-AU,en;q=0.9"
    assert env["webdriver"] is False
    assert env["getter"] == "function webdriver() {\n    [native code]\n}"
    assert env["getterName"] == "get webdriver" and env["wrongThis"] == "TypeError"
    assert env["toString"] == "function toString() {\n    [native code]\n}" and env["toStringName"] == "toString"
    assert env["own"] == "function own() { return 1; }"


async def test_a_block_page_leaves_the_site_alone(service, site, tmp_path):
    """Nothing on a block page to solve, and a retry only adds to the score:
    the site is refused for the rest of the day, and the result says so."""
    site.pages["/"] = (403, {"Server": "AkamaiGHost"},
                       "<title>Access Denied</title><h1>Access Denied</h1>"
                       "<p>You don't have permission to access this server.</p><p>Reference #18.abc</p>")
    site.pages["/other"] = PAGE
    svc = await service()
    r = await svc.browse(site.base + "/", "t")
    assert r.challenge["kind"] == "akamai_block"
    assert "127.0.0.1 is left alone until" in r.challenge["note"]
    assert svc.pending is None  # nothing for challenge() to act on
    with pytest.raises(ToolRefused, match="no challenge"):
        await svc.challenge("screenshot", "t")
    seen = len(site.requests)
    with pytest.raises(ToolRefused, match=r"left alone until .*: it answered with a block page \(akamai_block\)"):
        await svc.browse(site.base + "/other", "t")
    assert len(site.requests) == seen
    lines = [json.loads(l) for l in (tmp_path / "pages.log").read_text().splitlines()]
    blocked = [l for l in lines if l["outcome"] == "challenge"]
    assert len(blocked) == 1 and blocked[0]["challenge"] == "akamai_block"
    assert "left alone until" in blocked[0]["detail"]
    assert lines[-1]["outcome"] == "refused:limit"


async def test_a_site_min_interval_spaces_its_page_loads(service, site, tmp_path):
    import time
    site.pages["/"] = PAGE
    svc = await service(config(tmp_path, site_min_interval=3.0))
    await svc.browse(site.base + "/", "t")
    t0 = time.monotonic()
    await svc.browse(site.base + "/", "t")
    # The second load waited out the rest of the 3 s since the first.
    first, second = [json.loads(l)["time"] for l in (tmp_path / "pages.log").read_text().splitlines()]
    assert time.monotonic() - t0 >= 1.0
    from datetime import datetime
    assert (datetime.fromisoformat(second) - datetime.fromisoformat(first)).total_seconds() >= 3.0


async def test_a_private_address_is_refused_before_loading(service, private_site):
    private_site.pages["/"] = "secret"
    svc = await service()
    with pytest.raises(ToolRefused, match="denied network"):
        await svc.browse(private_site.base + "/", "t")
    assert private_site.requests == []


async def test_a_redirect_to_a_private_address_is_refused(service, site, private_site):
    private_site.pages["/"] = "secret"
    site.pages["/go"] = (302, {"Location": private_site.base + "/"}, "")
    svc = await service()
    with pytest.raises(ToolRefused, match="127.0.0.2"):
        await svc.browse(site.base + "/go", "t")
    assert private_site.requests == []


async def test_subresources_on_private_addresses_are_blocked(service, site, private_site):
    private_site.pages["/x.png"] = "nope"
    site.pages["/"] = f'<title>t</title><p>hi</p><img src="{private_site.base}/x.png"><script src="{private_site.base}/x.png"></script>'
    svc = await service()
    r = await svc.browse(site.base + "/", "t")
    assert r.text == "hi"
    assert private_site.requests == []


async def test_deny_listed_urls_are_refused(service, site):
    site.pages["/login"] = "login"
    svc = await service()
    with pytest.raises(ToolRefused, match="deny list"):
        await svc.browse(site.base + "/login", "t")
    assert site.requests == []
    # And a page that redirects to one.
    site.pages["/item"] = (302, {"Location": "/login"}, "")
    with pytest.raises(ToolRefused, match="ended up at"):
        await svc.browse(site.base + "/item", "t")


async def test_limits_refuse_and_say_when(service, site, tmp_path):
    site.pages["/"] = PAGE
    svc = await service(config(tmp_path, per_site_per_day=1))
    await svc.browse(site.base + "/", "t")
    with pytest.raises(ToolRefused, match=r"1 pages a day on 127.0.0.1: refused; it frees up at"):
        await svc.browse(site.base + "/", "t")


async def test_site_search_types_into_the_first_search_box(service, site):
    site.pages["/"] = """<title>Shop</title><form action="/find"><input type="text" name="other">
        <input type="search" name="term"></form><a href="/x">x</a>"""
    site.pages["/find"] = lambda q: f"<title>Results</title><p>You searched for {q['term'][0]}</p>"
    svc = await service()
    r = await svc.site_search(site.base, "ddr4 32gb", "t")
    assert r.title == "Results" and r.text == "You searched for ddr4 32gb"
    assert r.final_url.startswith(site.base + "/find?") and "term=ddr4+32gb" in r.final_url


async def test_site_search_recipe(service, site, tmp_path):
    site.pages["/start"] = '<title>Start</title><form action="/s"><input id="box" name="k"></form>'
    site.pages["/s"] = lambda q: f"<title>Found</title><p>{q['k'][0]}</p>"
    cfg = config(tmp_path)
    cfg.sites["shop"] = Site(url=site.base + "/start", input="#box")
    svc = await service(cfg)
    r = await svc.site_search("shop", "widgets", "t")
    assert r.text == "widgets"
    assert r.note is None and "note" not in r.as_dict()
    lines = [json.loads(l) for l in (tmp_path / "pages.log").read_text().splitlines()]
    assert [l["outcome"] for l in lines] == ["ok", "ok"]


async def test_a_stale_recipe_falls_back_to_the_generic_search_box(service, site, tmp_path):
    """The recipe's selector is gone (Scorptec's SLI box became Unbxd's
    input[name="q"]): the generic box is used, and logged, counted and noted."""
    site.pages["/start"] = '<title>Start</title><form action="/search"><input id="new" name="q"></form>'
    site.pages["/search"] = lambda q: f"<title>Found</title><p>{q['q'][0]}</p>"
    cfg = config(tmp_path)
    cfg.sites["shop"] = Site(url=site.base + "/start", input="#sli_search_1")
    svc = await service(cfg)
    r = await svc.site_search("shop", "widgets", "t")
    assert r.text == "widgets"
    assert r.note.startswith("recipe_stale: shop's input selector #sli_search_1 matched nothing")
    assert 'input[name="q"]' in r.note and r.as_dict()["note"] == r.note
    lines = [json.loads(l) for l in (tmp_path / "pages.log").read_text().splitlines()]
    assert [l["outcome"] for l in lines] == ["ok", "recipe_stale"]
    assert lines[-1]["detail"] == r.note
    assert 'browser_mcp_pages_total{outcome="recipe_stale",tool="site_search"} 1.0' in \
        svc.pagelog.exposition().decode()


async def test_a_broken_recipe_says_recipe_broken(service, site, tmp_path):
    site.pages["/start"] = '<title>Start</title><p>no search here</p>'
    cfg = config(tmp_path)
    cfg.sites["shop"] = Site(url=site.base + "/start", input="#sli_search_1")
    svc = await service(cfg)
    with pytest.raises(ToolRefused, match=r"^recipe_broken: shop's search box isn't on .*/start: "
                                          r"neither its input selector \(#sli_search_1\)"):
        await svc.site_search("shop", "widgets", "t")
    lines = [json.loads(l) for l in (tmp_path / "pages.log").read_text().splitlines()]
    assert [l["outcome"] for l in lines] == ["ok", "recipe_broken"]
    assert 'browser_mcp_pages_total{outcome="recipe_broken",tool="site_search"} 1.0' in \
        svc.pagelog.exposition().decode()
    # A host name with no recipe has none to be broken: the refusal is as it was.
    del cfg.sites["shop"]
    with pytest.raises(ToolRefused, match="^no search box found on"):
        await svc.site_search(site.base + "/start", "widgets", "t")


async def test_search_with_a_configured_engine(service, site, tmp_path):
    site.pages["/"] = '<title>Engine</title><form action="/results"><textarea name="q"></textarea></form>'
    site.pages["/results"] = lambda q: f"""<title>{q['q'][0]}</title><ol>
        <li class="r"><h3>First</h3><a href="https://one.example/">link</a><p>about one</p></li>
        <li class="r"><h3>Second</h3><a href="https://two.example/a?utm_source=x">link</a><p>about two</p></li></ol>"""
    cfg = config(tmp_path)
    cfg.engines["local"] = Engine(url=site.base + "/", input='textarea[name="q"]', result="li.r",
                                  title="h3", link="a", snippet="p")
    svc = await service(cfg)
    out, _ = await svc.search("ddr4 32gb price", "t", "local")
    assert out["results"] == [
        {"title": "First", "url": "https://one.example/", "snippet": "about one"},
        {"title": "Second", "url": "https://two.example/a?utm_source=x", "snippet": "about two"},
    ]


async def test_an_interstitial_is_waited_out(service, site):
    site.pages["/"] = (503, {"cf-mitigated": "challenge"},
                       "<title>Just a moment...</title><div id='challenge-running'></div>"
                       "<script>setTimeout(() => location.href = '/ok', 1500)</script>")
    site.pages["/ok"] = "<title>Real</title><p>the page</p>"
    svc = await service()
    r = await svc.browse(site.base + "/", "t")
    assert r.challenge is None and r.text == "the page" and r.status == 200


async def test_a_challenge_that_stays_is_reported(service, site, tmp_path):
    site.pages["/"] = (403, {"cf-mitigated": "challenge"}, "<title>Just a moment...</title><p>wait</p>")
    svc = await service()
    r = await svc.browse(site.base + "/", "t")
    assert r.challenge["kind"] == "cloudflare_interstitial"
    assert r.screenshot
    assert json.loads((tmp_path / "pages.log").read_text().splitlines()[-1])["challenge"] == "cloudflare_interstitial"


async def test_the_browser_closes_when_idle_and_keeps_the_profile(service, site, tmp_path):
    import asyncio
    site.pages["/"] = PAGE
    cfg = config(tmp_path)
    cfg.browser.idle_timeout = 2
    svc = await service(cfg)
    await svc.browse(site.base + "/", "t")
    assert svc.browser.running
    for _ in range(20):
        await asyncio.sleep(0.5)
        if not svc.browser.running:
            break
    assert not svc.browser.running
    assert (svc.browser.profile / "prefs.js").exists()
    await svc.browse(site.base + "/", "t")
    assert svc.browser.running
