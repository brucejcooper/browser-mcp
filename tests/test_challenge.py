"""The challenge tool against a stand-in widget on local pages, in a real
Firefox: confined to the widget's frame, refused outside a challenge, and the
live view handed to a person (with the real x11vnc) closing as it should."""

import asyncio
import json
import os
import shutil

import httpx
import pytest
from websockets.asyncio.client import connect as ws_connect

from browser_mcp.config import Config
from browser_mcp.limits import LimitExceeded
from browser_mcp.server import build
from browser_mcp.tools import ToolRefused
from test_browser import config
from test_liveview import listening

needs_browser = pytest.mark.skipif(not (shutil.which("Xvfb") and shutil.which("certutil")),
                                   reason="needs Xvfb and certutil")
needs_view = pytest.mark.skipif(not (shutil.which("x11vnc") and os.environ.get("BROWSER_MCP_NOVNC_DIR")),
                                reason="needs x11vnc and noVNC (the dev shell has them)")

# A stand-in for an hCaptcha widget: a frame whose URL reads as hCaptcha's,
# with a checkbox that does nothing, an answer box and a Verify button that
# fills in the page's response token when the answer is "cat".
WIDGET = """<!doctype html><body style="margin:0;background:#eef">
<div id="checkbox" style="position:absolute;left:10px;top:10px;width:24px;height:24px;border:2px solid #333"></div>
<input id="answer" style="position:absolute;left:20px;top:60px;width:200px;height:30px">
<button id="verify" style="position:absolute;left:20px;top:120px;width:100px;height:30px">Verify</button>
<script>
// The clicks, in the DOM: Camoufox runs Playwright's scripts in a world of
// their own, which sees the page's DOM but not its globals.
document.addEventListener('mousedown', e => document.body.dataset.click = [e.clientX, e.clientY]);
document.getElementById('verify').onclick = () => {
  if (document.getElementById('answer').value === 'cat')
    parent.document.querySelector('[name="h-captcha-response"]').value = 'solved';
};
</script>"""
CHALLENGE = (403, {}, """<!doctype html><title>Are you a robot?</title>
<p>Please complete the security check to go on.</p>
<iframe src="/hcaptcha.com/checkbox" style="width:400px;height:300px;border:0"></iframe>
<textarea name="h-captcha-response" style="display:none"></textarea>""")


def cfg_with_view(tmp_path, **view) -> Config:
    cfg = config(tmp_path)
    cfg.view.novnc_dir = os.environ.get("BROWSER_MCP_NOVNC_DIR", "")
    for k, v in view.items():
        setattr(cfg.view, k, v)
    return cfg


@pytest.fixture
async def service(tmp_path):
    services = []

    async def make(cfg=None):
        _, svc = build(cfg or cfg_with_view(tmp_path))
        services.append(svc)
        return svc

    yield make
    for svc in services:
        await svc.end_handoff()
        await svc.browser.close()
        await svc.egress.stop()


def actions(tmp_path):
    lines = [json.loads(l) for l in (tmp_path / "pages.log").read_text().splitlines()]
    return [l for l in lines if l["tool"] == "challenge"]


async def widget_frame(svc):
    page = await svc.browser.page()
    return next(f for f in page.frames if "hcaptcha.com" in f.url)


async def test_every_action_is_refused_without_a_challenge(tmp_path):
    _, svc = build(config(tmp_path))
    for action, kw in (("click", {"x": 10, "y": 10}), ("type", {"text": "x"}), ("screenshot", {}),
                       ("ask_human", {}), ("resume", {})):
        with pytest.raises(ToolRefused, match="no challenge to act on"):
            await svc.challenge(action, "t", **kw)
    assert not svc.browser.running  # nothing was even started
    logged = actions(tmp_path)
    assert [l["action"] for l in logged] == ["click", "type", "screenshot", "ask_human", "resume"]
    assert {l["outcome"] for l in logged} == {"refused:no_challenge"}


async def _challenge_page(svc, site):
    site.pages["/"] = CHALLENGE
    site.pages["/hcaptcha.com/checkbox"] = WIDGET
    r = await svc.browse(site.base + "/", "t")
    assert r.challenge["kind"] == "hcaptcha", r
    return r


@pytest.mark.browser
@needs_browser
async def test_the_client_solves_it_inside_the_widget_only(service, site, tmp_path):
    svc = await service()
    await _challenge_page(svc, site)

    out, shot = await svc.challenge("screenshot", "t")
    assert out["frame"]["width"] == 400 and out["frame"]["height"] == 300 and shot

    # Outside the frame's box, and from another caller: refused.
    for x, y in ((450, 10), (-1, 10), (10, 300)):
        with pytest.raises(ToolRefused, match="outside the widget's frame"):
            await svc.challenge("click", "t", x=x, y=y)
    with pytest.raises(ToolRefused, match="another caller"):
        await svc.challenge("click", "someone else", x=10, y=10)
    # Typing needs the focus on an input inside the frame.
    with pytest.raises(ToolRefused, match="click the input first"):
        await svc.challenge("type", "t", text="cat")

    out, _ = await svc.challenge("click", "t", x=100, y=75)
    assert out["challenge"] == "hcaptcha"
    frame = await widget_frame(svc)
    cx, cy = map(float, (await frame.evaluate("document.body.dataset.click")).split(","))
    assert abs(cx - 100) <= 1 and abs(cy - 75) <= 1  # where it was asked, in the frame's own pixels
    out, _ = await svc.challenge("type", "t", text="cat")
    assert await frame.evaluate("document.getElementById('answer').value") == "cat"
    with pytest.raises(ToolRefused, match="still there"):
        await svc.challenge("resume", "t")

    out, _ = await svc.challenge("click", "t", x=70, y=135)
    assert out["challenge"] is None
    r, _ = await svc.challenge("resume", "t")
    assert r["title"] == "Are you a robot?" and "security check" in r["text"] and r["untrusted"]
    # It's over: nothing more to act on.
    with pytest.raises(ToolRefused, match="no challenge to act on"):
        await svc.challenge("click", "t", x=100, y=75)

    logged = actions(tmp_path)
    assert [(l["action"], l["outcome"]) for l in logged] == [
        ("screenshot", "ok"), ("click", "refused:outside"), ("click", "refused:outside"),
        ("click", "refused:outside"), ("click", "refused:caller"), ("type", "refused:focus"),
        ("click", "ok"), ("type", "ok"), ("resume", "refused:still_there"), ("click", "ok"),
        ("resume", "ok"), ("click", "refused:no_challenge")]
    assert "cat" not in json.dumps(logged)  # what was typed isn't logged, only its length


@pytest.mark.browser
@needs_browser
async def test_the_next_page_load_ends_the_challenge(service, site):
    svc = await service()
    await _challenge_page(svc, site)
    site.pages["/other"] = "<title>Other</title><p>fine</p>"
    await svc.browse(site.base + "/other", "t")
    with pytest.raises(ToolRefused, match="no challenge to act on"):
        await svc.challenge("click", "t", x=100, y=75)


async def wait_for(cond, timeout=20.0):
    for _ in range(int(timeout * 10)):
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


@pytest.mark.browser
@needs_browser
@needs_view
async def test_a_person_solves_it_in_the_live_view(service, site, tmp_path):
    svc = await service()
    await _challenge_page(svc, site)
    before = listening()
    # Calls that come while ask_human is handing the browser over wait for it,
    # and are then refused: they mustn't take the browser from the person.
    asking = asyncio.create_task(svc.challenge("ask_human", "t"))
    await wait_for(svc.browser.lock.locked)
    racing = [asyncio.create_task(svc.browse(site.base + "/elsewhere", "another caller")),
              asyncio.create_task(svc.site_search(site.base, "x", "another caller")),
              asyncio.create_task(svc.challenge("click", "t", x=100, y=75))]
    out, _ = await asking
    for task in racing:
        with pytest.raises(ToolRefused, match="a person is solving"):
            await task
    assert svc.pending.with_human
    assert not any(path.startswith("/elsewhere") for path, _ in site.requests)
    link = out["link"]
    assert link.startswith("http://127.0.0.1:") and out["mode"] == "local"
    port = svc.pending.view.port
    assert listening() - before == {port}  # the view's port, and no VNC port

    async with httpx.AsyncClient() as person, httpx.AsyncClient() as other:
        r = await person.get(link)
        assert r.status_code == 200
        assert (await other.get(link)).status_code == 403  # a second open
        cookie = r.cookies["browser_mcp_view"]
        async with ws_connect(link.replace("http:", "ws:") + "websockify", subprotocols=["binary"],
                              additional_headers={"Cookie": f"browser_mcp_view={cookie}"}) as ws:
            assert (await ws.recv()).startswith(b"RFB 003.")  # the real x11vnc, on the browser's display

    # The browser is the person's meanwhile.
    with pytest.raises(ToolRefused, match="a person is solving"):
        await svc.browse(site.base + "/", "t")
    with pytest.raises(ToolRefused, match="a person is solving"):
        await svc.challenge("click", "t", x=100, y=75)
    out, _ = await svc.challenge("resume", "t")
    assert out["status"] == "with_human"

    # The person solves it; the view closes itself.
    page = await svc.browser.page()
    await page.evaluate("document.querySelector('[name=\"h-captcha-response\"]').value = 'solved'")
    await wait_for(lambda: svc.pending.ended is not None)
    assert svc.pending.ended == "cleared"
    assert listening() - before == set()
    r, _ = await svc.challenge("resume", "t")
    assert "security check" in r["text"]
    ended = [l for l in actions(tmp_path) if l["outcome"].startswith("ended:")]
    assert ended[-1]["outcome"] == "ended:cleared"
    assert link.split("/view/")[1].strip("/") not in (tmp_path / "pages.log").read_text()  # the token isn't logged


@pytest.mark.browser
@needs_browser
@needs_view
async def test_the_live_view_times_out_and_the_site_is_left_alone(service, site, tmp_path):
    svc = await service(cfg_with_view(tmp_path, timeout=3))
    await _challenge_page(svc, site)
    before = listening()
    out, _ = await svc.challenge("ask_human", "t")
    await wait_for(lambda: svc.pending.ended is not None)
    assert svc.pending.ended == "timeout"
    assert listening() - before == set()
    with pytest.raises(ToolRefused, match="timed out"):
        await svc.challenge("resume", "t")
    with pytest.raises(ToolRefused, match="left alone until"):
        await svc.browse(site.base + "/", "t")


@pytest.mark.browser
@needs_browser
@needs_view
async def test_done_with_the_challenge_still_there_fails(service, site, tmp_path):
    """The person opens the link in their own browser: noVNC connects, they press Done."""
    from playwright.async_api import async_playwright
    svc = await service()
    await _challenge_page(svc, site)
    out, _ = await svc.challenge("ask_human", "t")
    async with async_playwright() as pw:
        theirs = await pw.firefox.launch(headless=True)
        try:
            page = await theirs.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            await page.goto(out["link"])
            # noVNC draws the remote screen (1920 wide) into its canvas once connected.
            await page.wait_for_function(
                "() => { const c = document.querySelector('#screen canvas'); return c && c.width >= 1000; }",
                timeout=15000)
            assert not errors, errors
            await page.click("#done")
            await page.wait_for_function("() => document.getElementById('status').textContent.startsWith('Done')")
        finally:
            await theirs.close()
    await wait_for(lambda: svc.pending.ended is not None)
    assert svc.pending.ended == "done"
    # Left alone already, whether or not the client ever calls resume.
    with pytest.raises(LimitExceeded, match="left alone until"):
        svc.limits.check("anyone", "127.0.0.1")
    assert actions(tmp_path)[-1]["outcome"] == "ended:done" and "still there" in actions(tmp_path)[-1]["detail"]
    with pytest.raises(ToolRefused, match="already been handed"):
        await svc.challenge("ask_human", "t")  # one hand-off per challenge
    with pytest.raises(ToolRefused, match="still there; .* left alone until"):
        await svc.challenge("resume", "t")
    with pytest.raises(ToolRefused, match="left alone until"):
        await svc.browse(site.base + "/", "t")
