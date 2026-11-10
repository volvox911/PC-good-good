"""OPT-IN live tests against the real services. Never run by default.

    set WGO_RUN_LIVE=1 and the credentials, then:  python -m pytest -m live tests/live -v

* Anthropic: identifies rendered fixture labels with the real Claude model (costs a few cents).
* PriceCharting: one search + one product lookup (2 API calls, rate-limited).
* OBS: connects to your running OBS and screenshots the camera source
  (set WGO_LIVE_OBS_SOURCE to the camera input name; optional host/port/password).

These are the only tests that prove the live integrations work; a skip is NOT a pass.
"""

from __future__ import annotations

import io
import os

import pytest

from gameoverlay.config import RecognitionSettings
from gameoverlay.db import Database
from gameoverlay.demo.scene import DemoItem, render_box
from gameoverlay.pipeline.matcher import title_score
from gameoverlay.vision.platforms import canonical_base

from ..conftest import LIVE_ENV

pytestmark = [pytest.mark.live]

RUN = os.environ.get("WGO_RUN_LIVE") == "1"


def need(*keys):
    missing = [k for k in keys if not LIVE_ENV.get(k)]
    if not RUN:
        pytest.skip("live tests are opt-in (set WGO_RUN_LIVE=1)")
    if missing:
        pytest.skip(f"missing credentials: {', '.join(missing)}")


FIXTURES = [
    (DemoItem(None, "SUPER MARIO 64", "NINTENDO 64", "Nintendo 64", color=(200, 40, 40)), "Super Mario 64", "Nintendo 64"),
    (DemoItem(None, "MORTAL KOMBAT", "SEGA GENESIS", "Sega Genesis", color=(15, 15, 15)), "Mortal Kombat", "Sega Genesis"),
    (DemoItem(None, "RESIDENT EVIL", "PlayStation", "Playstation", color=(30, 30, 30)), "Resident Evil", "Playstation"),
]


@pytest.mark.parametrize("item,title,platform", FIXTURES, ids=[f[1] for f in FIXTURES])
async def test_live_claude_identifies_fixture_labels(tmp_path, item, title, platform):
    need("ANTHROPIC_API_KEY")
    from gameoverlay.vision.recognizer import ClaudeRecognizer
    from gameoverlay.vision.usage import UsageTracker

    db = Database(tmp_path / "live.sqlite3")
    cfg = RecognitionSettings()
    rec = ClaudeRecognizer(lambda: LIVE_ENV["ANTHROPIC_API_KEY"], lambda: cfg, UsageTracker(db, "live", lambda: cfg))
    buf = io.BytesIO()
    render_box(item).save(buf, "JPEG", quality=92)
    out = await rec.recognize([buf.getvalue()])
    assert out.ok, out.error
    assert out.result.item_type == "video_game"
    assert title_score(out.result.title or "", title) >= 85, out.result.title
    assert canonical_base(out.result.platform) == platform, out.result.platform
    print(f"latency {out.latency_ms} ms, est. cost ${out.cost_usd:.4f}")
    db.close()


async def test_live_pricecharting_search_and_product():
    if not LIVE_ENV.get("PRICECHARTING_TOKEN") and LIVE_ENV.get("PRICECHARTING_API_TOKEN"):
        LIVE_ENV["PRICECHARTING_TOKEN"] = LIVE_ENV["PRICECHARTING_API_TOKEN"]
    need("PRICECHARTING_TOKEN")
    from gameoverlay.pricing.client import PriceChartingClient
    from gameoverlay.pricing.ratelimit import SerialRateLimiter

    client = PriceChartingClient(lambda: LIVE_ENV["PRICECHARTING_TOKEN"], SerialRateLimiter(1.2))
    try:
        res = await client.search("super mario 64 nintendo 64")
        assert res, "no search results"
        n64 = next(c for c in res if c.console_name == "Nintendo 64")
        p = await client.product(n64.product_id)
        assert p.product_id == n64.product_id
        assert any(v is not None for v in (p.loose_cents, p.cib_cents, p.new_cents))
        print(p.to_dict())
    finally:
        await client.aclose()


async def test_live_obs_camera_screenshot():
    if not RUN:
        pytest.skip("live tests are opt-in (set WGO_RUN_LIVE=1)")
    source = LIVE_ENV.get("WGO_LIVE_OBS_SOURCE")
    if not source:
        pytest.skip("set WGO_LIVE_OBS_SOURCE to your camera input name")
    from PIL import Image

    from gameoverlay.obs.client import OBSClient

    host = LIVE_ENV.get("WGO_LIVE_OBS_HOST") or "127.0.0.1"
    port = int(LIVE_ENV.get("WGO_LIVE_OBS_PORT") or 4455)
    pw = LIVE_ENV.get("OBS_PASSWORD") or LIVE_ENV.get("OBS_WEBSOCKET_PASSWORD")
    obs = OBSClient(lambda: (host, port), lambda: pw)
    obs.start()
    try:
        assert await obs.wait_connected(8), obs.last_error
        names = [i["name"] for i in await obs.list_inputs()]
        assert source in names, names
        img = Image.open(io.BytesIO(await obs.screenshot(source, width=1280)))
        assert img.width >= 320
    finally:
        await obs.stop()
