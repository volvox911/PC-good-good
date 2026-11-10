"""Overlay (the operator's Joe Dirt price strip) in real Chromium: transparency, the slim top
strip at 1080x1920 / 720x1280 / 1920x1080 / 360px phones, long titles and high prices that
fit, whole-dollar rounding, N/A, atomic updates that never move the frame or animate,
idle strip between games, malformed input, demo watermark, bundled fonts, the container-unit
fallback for older OBS, WebSocket reconnection and self-reload after an app update."""

from __future__ import annotations

import io
import json
import re
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from .conftest import ROOT, SHOT_DIR, FixtureServer

pytestmark = pytest.mark.browser

DOCS_SHOTS = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
SIZES = [(1080, 1920), (720, 1280), (1920, 1080), (360, 640)]
CASES = {
    "standard": "900101",  # Zelda Ocarina of Time — mock data
    "long-title": "900801",  # Teenage Mutant Ninja Turtles II The Arcade Game
    "six-figure": "900601",  # six-figure NEW price
    "low-values": "900701",  # under $2
    "missing-na": "900501",  # NEW missing -> N/A
}
IDS = "['game-title','game-system','price-loose','price-complete','price-new'].map(i => document.getElementById(i).textContent)"

MEASURE = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
  const insideX = (a, b) => a[0] >= b[0] - 1 && a[2] <= b[2] + 1;
  const inside = (a, b) => insideX(a, b) && a[1] >= b[1] - 1 && a[3] <= b[3] + 1;
  const header = document.getElementById('header');
  const hb = box(header);
  const px = (el) => parseFloat(getComputedStyle(el).fontSize);
  const line = (id, frameSel) => {
    const el = document.getElementById(id);
    const frame = el.closest(frameSel);
    return { text: el.textContent, insideX: insideX(box(el), box(frame)), insideStrip: inside(box(el), hb), px: px(el),
             width: el.getBoundingClientRect().width };
  };
  return {
    header: hb,
    skin: box(document.querySelector('.frame-skin')),
    title: line('game-title', '.title-frame'),
    system: line('game-system', '.system-frame'),
    prices: ['price-loose', 'price-complete', 'price-new'].map((id) => line(id, '.value-plate')),
    labelPx: px(document.querySelector('.plate-label')),
    watermark: !document.getElementById('demoMark').hidden,
    overlay: document.body.dataset.overlay,
  };
}"""


def wait_ready(page, timeout=10000):
    page.wait_for_function("document.body.dataset.overlay === 'visible'", timeout=timeout)
    page.evaluate("document.fonts.ready.then(() => true)")
    page.wait_for_timeout(120)  # one more frame for the post-font refit


def show(server: FixtureServer, pid: str):
    server.api("POST", "/api/select", {"productId": pid})


def wait_for_product(page, pid: str):
    page.wait_for_function(f"window.__overlayState().shown && window.__overlayState().shown.productId === '{pid}'")


def alpha_at(png: bytes, xy) -> int:
    return Image.open(io.BytesIO(png)).convert("RGBA").getpixel(xy)[3]


def test_idle_strip_before_first_record(server, page_factory):
    """Default: the strip is on screen from the start, but shows no game and no prices."""
    server.api("POST", "/api/overlay/clear")
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    page.wait_for_function("document.body.dataset.content === 'idle'")
    wait_ready(page)
    assert page.evaluate(IDS) == ["Price Guide", "", "—", "—", "—"]
    png = page.screenshot(omit_background=True)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    (SHOT_DIR / "idle_1080x1920.png").write_bytes(png)
    assert alpha_at(png, (540, 90)) > 0, "idle strip is visible"
    assert alpha_at(png, (540, 960)) == 0, "the rest of the frame stays transparent"
    assert page.errors == []


def test_nothing_rendered_when_idle_panel_disabled(server, page_factory):
    server.api("POST", "/api/overlay/clear")
    server.api("PATCH", "/api/settings", {"overlay": {"idle_panel": False}})
    try:
        page = page_factory(1080, 1920)
        page.goto(server.base + "/overlay")
        page.wait_for_function("document.body.dataset.connection === 'open'")
        page.wait_for_timeout(500)
        img = Image.open(io.BytesIO(page.screenshot(omit_background=True))).convert("RGBA")
        assert img.getextrema()[3] == (0, 0), "fully transparent until a verified record arrives"
        assert page.evaluate(IDS) == ["", "", "", "", ""]
    finally:
        server.api("PATCH", "/api/settings", {"overlay": {"idle_panel": True}})


@pytest.mark.parametrize("w,h", SIZES)
def test_layouts_and_price_cases(server, page_factory, w, h):
    page = page_factory(w, h)
    page.goto(server.base + "/overlay")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    for name, pid in CASES.items():
        show(server, pid)
        wait_for_product(page, pid)
        wait_ready(page)
        m = page.evaluate(MEASURE)
        png = page.screenshot(omit_background=True)
        (SHOT_DIR / f"{name}_{w}x{h}.png").write_bytes(png)

        x0, y0, x1, y1 = m["header"]
        assert m["skin"] == m["header"], "the frame art fills the strip exactly"
        assert 0 <= x0 and x1 <= w and 0 <= y0, f"strip outside canvas: {m['header']}"
        assert y0 <= 12, f"strip sits at the very top (top at {y0})"
        assert abs((x1 - x0) / (y1 - y0) - 941 / 150) < 0.02, "frame keeps the design's proportions"
        if w < h:
            assert x0 <= 3.5 and x1 >= w - 3.5, "full width with the design's 3px margins"
            assert y1 <= h * 0.1, f"{name}: slim strip only (bottom at {y1})"
        else:
            assert x1 - x0 <= 1200.5 and y1 <= h * 0.2
        assert m["overlay"] == "visible" and m["watermark"] is False, "live records are never watermarked"
        # transparent everywhere except the strip
        assert alpha_at(png, (w // 2, int((y0 + y1) / 2))) > 0
        assert alpha_at(png, (w // 2, int(y1) + 6)) == 0, "nothing drawn below the strip"
        assert alpha_at(png, (w // 2, int(h * 0.5))) == 0, "central product area must stay clear"
        assert alpha_at(png, (w // 2, h - 3)) == 0, "bottom of the frame stays clear"
        # every piece of text stays inside its own frame or plate
        for part in [m["title"], m["system"], *m["prices"]]:
            assert part["insideX"] and part["insideStrip"], f"{name} at {w}x{h}: {part['text']!r} overflows"
        texts = [p["text"] for p in m["prices"]]
        if name == "standard":
            assert m["title"]["text"] == "Zelda Ocarina of Time" and m["system"]["text"] == "NINTENDO 64"
            assert texts == ["$43", "$110", "$599"], "whole dollars, rounded half-up"
        if name == "six-figure":
            assert texts == ["$980", "$12,500", "$123,457"]
        if name == "low-values":
            assert texts == ["$2", "$4", "$15"]
        if name == "missing-na":
            assert texts[2] == "N/A" and "$0" not in texts
        if w == 360:  # legibility on a phone
            assert min(p["px"] for p in m["prices"]) >= 16 and m["labelPx"] >= 8
    assert page.errors == []


@pytest.mark.parametrize("round_whole", [True, False])
def test_long_names_and_high_prices_fit(server, page_factory, round_whole):
    """The design package's QA list: long names and $1,245.99 fit without clipping."""
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay?preview=1")
    page.evaluate(f"window.postMessage({{type: 'preview-layout', layout: {{roundWholeDollars: {str(round_whole).lower()}}}}}, location.origin)")
    page.wait_for_function(f"window.__overlayState().layout.roundWholeDollars === {str(round_whole).lower()}")
    cases = [
        ("SUPER MARIO 64", "NINTENDO 64", [28, 64, 140]),
        ("THE LEGEND OF ZELDA: MAJORA'S MASK", "NINTENDO 64", ["$1,245.99", 3200, 10999]),
        ("STAR FOX 64", "NINTENDO 64", [24, 58, 200]),
        ("Teenage Mutant Ninja Turtles II: The Arcade Game — Collector's Limited Edition", "SEGA GENESIS / MEGA DRIVE (PAL)", [123456.78, 99999.99, 1245.99]),
        ("W" * 110, "W" * 60, [1, 2, 3]),  # absurd: shortened with an ellipsis, never spills over
    ]
    for title, system, (lo, co, ne) in cases:
        ok = page.evaluate("(a) => window.updateOverlay({title: a[0], system: a[1], prices: {loose: a[2], complete: a[3], new: a[4]}})",
                           [title, system, lo, co, ne])
        assert ok is True
        wait_ready(page)
        m = page.evaluate(MEASURE)
        for part in [m["title"], m["system"], *m["prices"]]:
            assert part["insideX"] and part["insideStrip"], f"{title[:30]}: {part['text']!r} overflows"
        assert m["watermark"] is True, "preview data is always marked as demo"
        if title.startswith("THE LEGEND"):
            assert m["title"]["text"] == title, "a real long name is squeezed, not shortened"
            assert m["prices"][0]["text"] == ("$1,246" if round_whole else "$1,245.99")
            if round_whole:
                (SHOT_DIR / "stress_1080x1920.png").write_bytes(page.screenshot(omit_background=True))
        if title.startswith("W"):
            assert m["title"]["text"].endswith("…")
    assert page.errors == []


def test_updates_never_move_the_frame_or_animate(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    show(server, "900101")
    wait_for_product(page, "900101")
    wait_ready(page)
    before = page.evaluate(MEASURE)
    page.evaluate("""() => {
      window.__moves = [];
      const hb = () => JSON.stringify(document.getElementById('header').getBoundingClientRect());
      const first = hb();
      new MutationObserver(() => { if (hb() !== first) window.__moves.push(hb()); })
        .observe(document.getElementById('header'), {subtree: true, childList: true, characterData: true});
    }""")
    for pid in ["900801", "900601", "900501", "900101"]:
        show(server, pid)
        wait_for_product(page, pid)
        assert page.evaluate("document.getAnimations().length") == 0, "updates never trigger a transition"
    server.api("POST", "/api/overlay/clear")
    page.wait_for_function("document.body.dataset.content === 'idle'")
    after = page.evaluate(MEASURE)
    assert after["header"] == before["header"] and after["skin"] == before["skin"]
    assert page.evaluate("window.__moves") == []
    durations = page.evaluate("[...document.querySelectorAll('#header, #header *')].map(e => getComputedStyle(e).transitionDuration)")
    assert set(durations) == {"0s"}


def test_atomic_updates_never_mix_title_and_prices(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    show(server, "900101")
    wait_for_product(page, "900101")
    wait_ready(page)
    page.evaluate("""() => {
      window.__snaps = [];
      const ids = ['game-title','game-system','price-loose','price-complete','price-new'];
      const snap = () => ids.map(i => document.getElementById(i).textContent).join('|');
      // what the viewer can see: the state at each animation frame
      const tick = () => { const s = snap(); if (window.__snaps[window.__snaps.length - 1] !== s) window.__snaps.push(s); requestAnimationFrame(tick); };
      tick();
    }""")
    for pid in ["900302", "900601", "900401", "900701", "900101", "900501"]:
        show(server, pid)
        page.wait_for_timeout(60)
    wait_for_product(page, "900501")
    page.wait_for_timeout(100)
    snaps = page.evaluate("window.__snaps")
    valid = {
        "ZELDA OCARINA OF TIME|NINTENDO 64|$43|$110|$599",
        "MORTAL KOMBAT|SUPER NINTENDO|$15|$47|$210",
        "STADIUM EVENTS|NINTENDO NES|$980|$12,500|$123,457",
        "RESIDENT EVIL|PLAYSTATION|$43|$69|$420",
        "MADDEN NFL 2004|PLAYSTATION 2|$2|$4|$15",
        "EARTHBOUND|SUPER NINTENDO|$172|$430|N/A",
    }
    assert len(snaps) >= 2, "expected visible updates"
    for s in snaps:
        title, console, *prices = s.split("|")
        key = f"{title.upper()}|{console}|{'|'.join(prices)}"
        assert key in valid, f"mixed or partial record rendered: {s}"


def test_hide_and_show(server, page_factory):
    page = page_factory(720, 1280)
    page.goto(server.base + "/overlay")
    show(server, "900302")
    wait_for_product(page, "900302")
    server.api("POST", "/api/overlay/clear")
    # The game left: the strip stays on screen, cleared — no title or prices from the old game.
    page.wait_for_function("document.body.dataset.content === 'idle'")
    assert page.evaluate(IDS) == ["Price Guide", "", "—", "—", "—"]
    assert page.evaluate("window.__overlayState().visible") is True
    show(server, "900401")
    wait_for_product(page, "900401")
    assert page.evaluate("document.getElementById('price-loose').textContent") == "$43"


def test_rounding_setting_is_applied_live(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    show(server, "900401")
    wait_for_product(page, "900401")
    try:
        server.api("PATCH", "/api/settings", {"overlay": {"round_whole_dollars": False}})
        page.wait_for_function("document.getElementById('price-loose').textContent === '$42.95'")
        assert page.evaluate(IDS)[2:] == ["$42.95", "$68.50", "$420"]
    finally:
        server.api("PATCH", "/api/settings", {"overlay": {"round_whole_dollars": True}})
    page.wait_for_function("document.getElementById('price-loose').textContent === '$43'")


def test_malformed_inputs_rejected(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    show(server, "900101")
    wait_for_product(page, "900101")
    results = page.evaluate("""() => [
      window.setGameOverlayData(null),
      window.setGameOverlayData({title: 'X', platform: 'NES', productId: '1', verified: false, retrievedAt: new Date().toISOString()}),
      window.setGameOverlayData({title: '', platform: 'NES', productId: '1', verified: true, retrievedAt: new Date().toISOString()}),
      window.setGameOverlayData({title: 'X', platform: 'NES', productId: '1', verified: true, looseCents: -1, retrievedAt: new Date().toISOString()}),
      window.setGameOverlayData({title: 'X', platform: 'NES', productId: '1', verified: true, looseUsd: 'free', retrievedAt: new Date().toISOString()}),
      window.setGameOverlayData({title: '<img src=x onerror=alert(1)>', platform: 'NES', productId: 'x', verified: true, retrievedAt: new Date().toISOString()}),
    ]""")
    assert results == [False] * 6
    assert page.evaluate("document.getElementById('price-loose').textContent") == "$43"
    assert page.evaluate("typeof window.updateOverlay") == "undefined", "unverified updates exist only in the preview"
    # Even a valid-shaped title is only ever rendered as text, never HTML.
    page.evaluate("window.setGameOverlayData({title: '<b>bold</b>', platform: 'NES', productId: '5', verified: true, looseUsd: 1, retrievedAt: new Date().toISOString()})")
    assert page.evaluate("document.getElementById('game-title').children.length") == 0
    assert page.evaluate("document.getElementById('game-title').textContent") == "<b>bold</b>"


def test_demo_preview_is_watermarked(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay?preview=1")
    sample = server.api("GET", "/api/preview/sample")
    page.evaluate("([r, l]) => window.postMessage({type: 'preview', record: r, layout: l}, location.origin)", [sample["record"], sample["layout"]])
    page.wait_for_function("document.body.dataset.content === 'record'")
    wait_ready(page)
    assert page.evaluate("!document.getElementById('demoMark').hidden") is True
    assert page.evaluate("document.body.dataset.connection") is None, "preview never opens the live socket"
    (SHOT_DIR / "demo_preview_1080x1920.png").write_bytes(page.screenshot(omit_background=True))


def test_exact_font_families_are_used(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    show(server, "900101")
    wait_for_product(page, "900101")
    wait_ready(page)
    loaded = page.evaluate("[...document.fonts].filter(f => f.status === 'loaded').map(f => `${f.family} ${f.weight} ${f.style}`)")
    for face in ["Knewave 400 normal", "Anton 400 normal", "Barlow Condensed 800 normal", "Barlow Condensed 900 italic"]:
        assert face in loaded, f"{face} not loaded: {loaded}"
    first = page.evaluate("""() => ['#game-title', '#game-system', '.plate-label', '#price-loose']
      .map(s => getComputedStyle(document.querySelector(s)).fontFamily.split(',')[0].replace(/["']/g, ''))""")
    assert first == ["Knewave", "Barlow Condensed", "Barlow Condensed", "Anton"]


def test_container_unit_fallback_matches_for_older_obs(server, page_factory):
    """OBS builds before Chromium 105 lack `cqw`; the fallback rules must render identically."""
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    show(server, "900801")
    wait_for_product(page, "900801")
    wait_ready(page)
    css = (ROOT / "gameoverlay" / "web" / "overlay" / "style.css").read_text(encoding="utf-8")
    block = re.search(r"@supports not \(width: 1cqw\) \{(.*)\}\s*$", css, re.S)
    assert block, "fallback block present"
    probe = """() => ['#game-title', '#game-system', '.plate-label', '#price-loose', '#price-new'].map(s => {
      const cs = getComputedStyle(document.querySelector(s));
      return [cs.fontSize, cs.letterSpacing, cs.textShadow];
    })"""
    native = page.evaluate(probe)
    page.add_style_tag(content=block.group(1))
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    page.wait_for_timeout(100)
    forced = page.evaluate(probe)

    def nums(v):
        return [round(float(x), 1) for x in re.findall(r"-?\d+\.?\d*", json.dumps(v))]

    assert nums(forced) == pytest.approx(nums(native), abs=0.15)


def test_websocket_reconnects_without_refresh(tmp_path, page_factory):
    srv = FixtureServer(tmp_path / "data").start()
    try:
        page = page_factory(1080, 1920)
        page.goto(srv.base + "/overlay")
        page.evaluate("window.__marker = 'same-page'")
        show(srv, "900101")
        wait_for_product(page, "900101")
        srv.stop()
        page.wait_for_function("document.body.dataset.connection === 'closed'", timeout=10000)
        page.wait_for_timeout(1500)
        # transient outage: the verified record stays up (within the disconnect-hide policy)
        assert page.evaluate("window.__overlayState().shown.productId") == "900101"
        srv2 = FixtureServer(tmp_path / "data", port=srv.port).start()
        try:
            page.wait_for_function("document.body.dataset.connection === 'open'", timeout=20000)
            # New backend has nothing on air yet -> overlay follows the server state (idle strip).
            page.wait_for_function("document.body.dataset.content === 'idle'", timeout=10000)
            show(srv2, "900302")
            wait_for_product(page, "900302")
            assert page.evaluate("window.__marker") == "same-page", "page must not have been reloaded"
        finally:
            srv2.stop()
    finally:
        srv.stop()


def test_overlay_clears_after_long_disconnect(tmp_path, page_factory):
    srv = FixtureServer(tmp_path / "data").start()
    try:
        srv.api("PATCH", "/api/settings", {"display": {"overlay_disconnect_hide_s": 5}})
        page = page_factory(1080, 1920)
        page.goto(srv.base + "/overlay")
        show(srv, "900101")
        wait_for_product(page, "900101")
        srv.stop()
        page.wait_for_function("document.body.dataset.connection === 'closed'", timeout=10000)
        start = time.time()
        # possibly-outdated prices are cleared; the strip itself stays (idle)
        page.wait_for_function("window.__overlayState().shown && window.__overlayState().shown.idle === true", timeout=15000)
        assert time.time() - start >= 3
        assert page.evaluate("document.getElementById('price-loose').textContent") == "—"
    finally:
        srv.stop()


def _app_updated_while_open(page, times: int | None):
    """Make the backend report a newer overlay build than the open page carries, as after
    an app update (for the first `times` connections; None = always)."""
    conns = {"n": 0}

    def handler(ws):
        conns["n"] += 1
        server = ws.connect_to_server()
        n = conns["n"]

        def from_server(message):
            if times is None or n <= times:
                msg = json.loads(message)
                if msg.get("type") == "state":
                    msg["build"] = "newer0build0"
                message = json.dumps(msg)
            ws.send(message)

        server.on_message(from_server)

    page.route_web_socket(re.compile(r".*/ws/overlay$"), handler)
    loads = {"n": 0}
    page.on("load", lambda _p: loads.__setitem__("n", loads["n"] + 1))
    return loads


def test_open_obs_page_reloads_itself_after_an_app_update(server, page_factory):
    """OBS keeps an open page running across app updates; an outdated page must reload once."""
    page = page_factory(1080, 1920)
    loads = _app_updated_while_open(page, times=1)
    page.goto(server.base + "/overlay")
    page.wait_for_function("window.__overlayState && document.body.dataset.overlay === 'visible'", timeout=10000)
    page.wait_for_timeout(800)
    assert loads["n"] == 2, "outdated page reloaded exactly once"
    assert page.evaluate("sessionStorage.getItem('overlayReloadedFor')") == "newer0build0"


def test_outdated_page_never_reload_loops(server, page_factory):
    page = page_factory(1080, 1920)
    loads = _app_updated_while_open(page, times=None)
    page.goto(server.base + "/overlay")
    page.wait_for_timeout(2500)
    assert loads["n"] <= 2, "at most one automatic reload per build"
    page.wait_for_function("document.body.dataset.connection === 'open'", timeout=10000)
    assert page.evaluate("document.body.dataset.overlay") == "visible", "keeps working on the old page"


def test_update_docs_screenshots():
    """Copies the main renders into docs/screenshots (set WGO_UPDATE_DOC_SCREENSHOTS=1)."""
    import os

    if os.environ.get("WGO_UPDATE_DOC_SCREENSHOTS") != "1":
        pytest.skip("set WGO_UPDATE_DOC_SCREENSHOTS=1 to refresh docs/screenshots")
    if not SHOT_DIR.exists():
        pytest.skip("no screenshots captured")
    DOCS_SHOTS.mkdir(parents=True, exist_ok=True)
    for f in ["standard_1080x1920.png", "long-title_1080x1920.png", "six-figure_1080x1920.png",
              "missing-na_720x1280.png", "standard_1920x1080.png", "low-values_720x1280.png",
              "standard_360x640.png", "stress_1080x1920.png", "idle_1080x1920.png", "demo_preview_1080x1920.png"]:
        src = SHOT_DIR / f
        if src.exists():
            shutil.copy(src, DOCS_SHOTS / f)
