"""Overlay rendering in real Chromium (14): transparency, sizes, long titles, six-figure /
low / N/A prices, atomic updates, hide/show, malformed inputs, demo watermark, WebSocket
reconnection without a page refresh."""

from __future__ import annotations

import io
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from .conftest import SHOT_DIR, FixtureServer

pytestmark = pytest.mark.browser

DOCS_SHOTS = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
SIZES = [(1080, 1920), (720, 1280), (1920, 1080)]
CASES = {
    "standard": "900101",  # Zelda Ocarina of Time — $42.50 / $109.99 / $599.00 (mock data)
    "long-title": "900801",  # Teenage Mutant Ninja Turtles II The Arcade Game
    "six-figure": "900601",  # $123,456.78 NEW
    "low-values": "900701",  # $1.99
    "missing-na": "900501",  # NEW missing -> N/A
}

MEASURE = """() => {
  const anchor = document.getElementById('anchor');
  const scale = parseFloat((anchor.style.transform.match(/scale\\(([^)]+)\\)/) || [0, 1])[1]);
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
  const inside = (a, b) => a[0] >= b[0] - 1 && a[1] >= b[1] - 1 && a[2] <= b[2] + 1 && a[3] <= b[3] + 1;
  const m = document.getElementById('machine');
  const prices = ['priceLoose', 'priceComplete', 'priceNew'].map((id) => {
    const el = document.getElementById(id);
    const glass = el.closest('.price-cell');
    return { text: el.textContent, inside: inside(box(el), box(glass)), overflow: el.getBoundingClientRect().width > glass.getBoundingClientRect().width,
             canvasPx: parseFloat(getComputedStyle(el).fontSize) * scale };
  });
  const t = document.getElementById('gameTitle');
  const plate = t.closest('.identity');
  return {
    machine: box(m), scale, prices,
    title: (() => {
      const range = document.createRange(); range.selectNodeContents(t);
      const rects = [...range.getClientRects()];
      const lines = new Set(rects.map((r) => Math.round(r.top))).size;
      const fs = parseFloat(getComputedStyle(t).fontSize);
      const textBottom = Math.max(...rects.map((r) => r.bottom));
      const tb = t.getBoundingClientRect();
      // clipped = some rendered text lies below the visible box (line-clamp) by more than a sliver
      return { text: t.textContent, lines, inside: inside(box(t), box(plate)),
               clipped: lines > 3 || textBottom > tb.bottom + 0.15 * fs * scale, canvasPx: fs * scale };
    })(),
    console: document.getElementById('consoleName').textContent,
    asOf: document.getElementById('asOf').textContent,
    labelPx: parseFloat(getComputedStyle(document.querySelector('.price-label')).fontSize) * scale,
    consolePx: parseFloat(getComputedStyle(document.getElementById('consoleName')).fontSize) * scale,
    watermark: !document.getElementById('demoMark').hidden,
    state: m.dataset.state,
  };
}"""


def wait_settled(page, visible=True, timeout=10000):
    want = "visible" if visible else "hidden"
    page.wait_for_function(
        f"document.body.dataset.overlay === '{want}' && document.body.dataset.settled !== '0'", timeout=timeout
    )
    page.wait_for_timeout(450)  # let entry/exit transitions finish


def show(server: FixtureServer, pid: str):
    server.api("POST", "/api/select", {"productId": pid})


def alpha_at(png: bytes, xy) -> int:
    return Image.open(io.BytesIO(png)).convert("RGBA").getpixel(xy)[3]


IDS = "['gameTitle','consoleName','priceLoose','priceComplete','priceNew'].map(i => document.getElementById(i).textContent)"


def test_idle_panel_before_first_record(server, page_factory):
    """Default: the band is on screen from the start, but shows no game and no prices."""
    server.api("POST", "/api/overlay/clear")
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    page.wait_for_function("document.body.dataset.content === 'idle'")
    wait_settled(page)
    assert page.evaluate(IDS) == ["Price Guide", "", "—", "—", "—"]
    png = page.screenshot(omit_background=True)
    assert alpha_at(png, (540, 120)) > 0, "idle band is visible"
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
        page.wait_for_function(f"window.__overlayState().shown && window.__overlayState().shown.productId === '{pid}'")
        wait_settled(page)
        m = page.evaluate(MEASURE)
        png = page.screenshot(omit_background=True)
        (SHOT_DIR / f"{name}_{w}x{h}.png").write_bytes(png)

        x0, y0, x1, y1 = m["machine"]
        assert 0 <= x0 and x1 <= w and 0 <= y0 and y1 <= h, f"machine outside canvas: {m['machine']}"
        assert m["state"] == "visible" and m["watermark"] is False, "live records are never watermarked"
        # transparent outside the machine
        assert alpha_at(png, (2, 2)) == 0 and alpha_at(png, (w - 3, 2)) == 0
        assert alpha_at(png, (w // 2, int(h * 0.5))) == 0, "central product area must stay clear"
        assert alpha_at(png, (w // 2, h - 3)) == 0, "bottom of the frame stays clear"
        # slim band anchored at the top by default
        assert y0 <= h * 0.05, f"band should sit at the top of the frame (top at {y0})"
        limit = 0.18 if w < h else 0.2  # incl. the top margin; two-line titles are the tallest case
        assert y1 <= h * limit, f"{name}: band should be slim (bottom at {y1}, {(y1 - y0) / h:.1%} of the height)"
        if w < h:
            margin = min(x0, w - x1)
            assert 2 <= margin <= w * 0.13, f"side margin {margin}"
        for p in m["prices"]:
            assert p["inside"] and not p["overflow"], f"{name}: price {p['text']} overflows its display"
        assert m["title"]["inside"] and not m["title"]["clipped"], f"{name}: title clipped"
        # legibility on a 360px-wide phone (vertical streams)
        if w < h:
            phone = 360 / w
            biggest = max(p["canvasPx"] for p in m["prices"])
            assert biggest * phone >= 16, f"{name}: prices too small for phones ({biggest * phone:.1f}px)"
            assert min(p["canvasPx"] for p in m["prices"]) * phone >= (9 if name == "six-figure" else 14)
            assert m["title"]["canvasPx"] * phone >= (7 if name == "long-title" else 12)
            assert m["labelPx"] * phone >= 7, "LOOSE/COMPLETE/NEW must stay readable on phones"
            assert m["consolePx"] * phone >= 7.5
        else:
            assert max(p["canvasPx"] for p in m["prices"]) >= 34
        assert m["asOf"] == "", "retrieval time is off by default"
        texts = [p["text"] for p in m["prices"]]
        if name == "standard":
            assert texts == ["$42.50", "$109.99", "$599"]  # whole dollars drop ".00" (same exact value)
            assert m["console"] == "NINTENDO 64"
        if name == "six-figure":
            assert texts[2] == "$123,456.78"
        if name == "low-values":
            assert texts[0] == "$1.99"
        if name == "missing-na":
            assert texts[2] == "N/A" and "$0.00" not in texts
    assert page.errors == []


def test_update_docs_screenshots():
    """Copies the main renders into docs/screenshots (set WGO_UPDATE_DOC_SCREENSHOTS=1)."""
    import os

    if os.environ.get("WGO_UPDATE_DOC_SCREENSHOTS") != "1":
        pytest.skip("set WGO_UPDATE_DOC_SCREENSHOTS=1 to refresh docs/screenshots")
    if not SHOT_DIR.exists():
        pytest.skip("no screenshots captured")
    DOCS_SHOTS.mkdir(parents=True, exist_ok=True)
    for f in ["standard_1080x1920.png", "long-title_1080x1920.png", "six-figure_1080x1920.png",
              "missing-na_720x1280.png", "standard_1920x1080.png", "low-values_720x1280.png"]:
        src = SHOT_DIR / f
        if src.exists():
            shutil.copy(src, DOCS_SHOTS / f)


def test_atomic_updates_never_mix_title_and_prices(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    show(server, "900101")
    page.wait_for_function("window.__overlayState().shown && window.__overlayState().shown.productId === '900101' && !window.__overlayState().busy")
    wait_settled(page)
    page.evaluate("""() => {
      window.__snaps = [];
      const ids = ['gameTitle','consoleName','priceLoose','priceComplete','priceNew'];
      const snap = () => ids.map(i => document.getElementById(i).textContent).join('|');
      new MutationObserver(() => window.__snaps.push(snap()))
        .observe(document.getElementById('machine'), {subtree: true, childList: true, characterData: true});
    }""")
    for pid in ["900302", "900601", "900401", "900701", "900101", "900501"]:
        show(server, pid)
        page.wait_for_timeout(60)  # updates arrive faster than the 300 ms animation
    page.wait_for_function("window.__overlayState().shown.productId === '900501' && !window.__overlayState().busy")
    snaps = page.evaluate("window.__snaps")
    valid = {
        "ZELDA OCARINA OF TIME|NINTENDO 64|$42.50|$109.99|$599",
        "MORTAL KOMBAT|SUPER NINTENDO|$14.50|$46.99|$210",
        "STADIUM EVENTS|NINTENDO NES|$980|$12,500|$123,456.78",
        "RESIDENT EVIL|PLAYSTATION|$42.95|$68.50|$420",
        "MADDEN NFL 2004|PLAYSTATION 2|$1.99|$3.75|$14.99",
        "EARTHBOUND|SUPER NINTENDO|$172.44|$429.95|N/A",
    }
    assert snaps, "expected DOM updates"
    for s in snaps:
        title, console, *prices = s.split("|")
        key = f"{title.upper()}|{console}|{'|'.join(prices)}"
        assert key in valid, f"mixed or partial record rendered: {s}"


def test_hide_and_show(server, page_factory):
    page = page_factory(720, 1280)
    page.goto(server.base + "/overlay")
    show(server, "900302")
    wait_settled(page)
    server.api("POST", "/api/overlay/clear")
    # The game left: the band stays on screen, cleared — no title or prices from the old game.
    page.wait_for_function("document.body.dataset.content === 'idle'")
    wait_settled(page)
    assert page.evaluate(IDS) == ["Price Guide", "", "—", "—", "—"]
    assert page.evaluate("window.__overlayState().visible") is True
    show(server, "900401")
    wait_settled(page)
    assert page.evaluate("document.getElementById('priceLoose').textContent") == "$42.95"


def test_malformed_inputs_rejected(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay")
    show(server, "900101")
    wait_settled(page)
    results = page.evaluate("""() => [
      window.setGameOverlayData(null),
      window.setGameOverlayData({title: 'X', platform: 'NES', productId: '1', verified: false, retrievedAt: new Date().toISOString()}),
      window.setGameOverlayData({title: '', platform: 'NES', productId: '1', verified: true, retrievedAt: new Date().toISOString()}),
      window.setGameOverlayData({title: 'X', platform: 'NES', productId: '1', verified: true, looseCents: -1, retrievedAt: new Date().toISOString()}),
      window.setGameOverlayData({title: 'X', platform: 'NES', productId: '1', verified: true, looseUsd: 'free', retrievedAt: new Date().toISOString()}),
      window.setGameOverlayData({title: '<img src=x onerror=alert(1)>', platform: 'NES', productId: 'x', verified: true, retrievedAt: new Date().toISOString()}),
    ]""")
    assert results == [False] * 6
    page.wait_for_timeout(400)
    assert page.evaluate("document.getElementById('priceLoose').textContent") == "$42.50"
    # Even a valid-shaped title is only ever rendered as text, never HTML.
    page.evaluate("window.setGameOverlayData({title: '<b>bold</b>', platform: 'NES', productId: '5', verified: true, looseUsd: 1, retrievedAt: new Date().toISOString()})")
    wait_settled(page)
    assert page.evaluate("document.getElementById('gameTitle').children.length") == 0


def test_demo_preview_is_watermarked(server, page_factory):
    page = page_factory(1080, 1920)
    page.goto(server.base + "/overlay?preview=1")
    sample = server.api("GET", "/api/preview/sample")
    page.evaluate("([r, l]) => window.postMessage({type: 'preview', record: r, layout: l}, location.origin)", [sample["record"], sample["layout"]])
    wait_settled(page)
    assert page.evaluate("!document.getElementById('demoMark').hidden") is True
    assert page.evaluate("document.body.dataset.connection") is None, "preview never opens the live socket"
    png = page.screenshot(omit_background=True)
    (SHOT_DIR / "demo_preview_1080x1920.png").write_bytes(png)


def test_reduced_motion_swaps_instantly(server, page_factory):
    page = page_factory(1080, 1920, reduced_motion=True)
    page.goto(server.base + "/overlay")
    show(server, "900101")
    page.wait_for_function("window.__overlayState().shown && window.__overlayState().shown.productId === '900101'")
    show(server, "900302")
    page.wait_for_function("window.__overlayState().shown.productId === '900302'", timeout=3000)
    assert page.evaluate("document.getElementById('machine').classList.contains('swap-out')") is False
    dur = page.evaluate("getComputedStyle(document.querySelector('.swap')).transitionDuration")
    assert dur in ("0s", "0s, 0s")


def test_websocket_reconnects_without_refresh(tmp_path, page_factory):
    srv = FixtureServer(tmp_path / "data").start()
    try:
        page = page_factory(1080, 1920)
        page.goto(srv.base + "/overlay")
        page.evaluate("window.__marker = 'same-page'")
        show(srv, "900101")
        wait_settled(page)
        srv.stop()
        page.wait_for_function("document.body.dataset.connection === 'closed'", timeout=10000)
        page.wait_for_timeout(1500)
        # transient outage: the verified record stays up (within the disconnect-hide policy)
        assert page.evaluate("window.__overlayState().visible") is True
        srv2 = FixtureServer(tmp_path / "data", port=srv.port).start()
        try:
            page.wait_for_function("document.body.dataset.connection === 'open'", timeout=20000)
            # New backend has nothing on air yet -> overlay follows the server state (idle band).
            page.wait_for_function("document.body.dataset.content === 'idle'", timeout=10000)
            show(srv2, "900302")
            page.wait_for_function("window.__overlayState().shown && window.__overlayState().shown.productId === '900302'")
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
        wait_settled(page)
        srv.stop()
        page.wait_for_function("document.body.dataset.connection === 'closed'", timeout=10000)
        start = time.time()
        # possibly-outdated prices are cleared; the band itself stays (idle)
        page.wait_for_function("window.__overlayState().shown && window.__overlayState().shown.idle === true", timeout=15000)
        assert time.time() - start >= 3
        assert page.evaluate("document.getElementById('priceLoose').textContent") == "—"
    finally:
        srv.stop()
