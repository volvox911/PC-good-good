"""Pipeline state machine: atomic updates, supersession, stale data, holds, corrections, soak."""

from __future__ import annotations

import asyncio
import gc
import json
import tracemalloc

import pytest

from gameoverlay.demo.mock_pricecharting import load_catalog
from gameoverlay.pipeline.engine import PipelineState
from gameoverlay.money import parse_cents

from .engine_harness import ITEMS, Harness, unknown_item

CATALOG = {p["id"]: p for p in load_catalog()}


@pytest.fixture
async def h(db):
    harness = Harness(db)
    yield harness
    await harness.close()


def assert_record_consistent(rec):
    """Requirement 19: title, platform and all prices belong to the same product."""
    p = CATALOG[rec.product_id]
    assert rec.title == p["product-name"]
    assert rec.loose_cents == parse_cents(p.get("loose-price"))
    assert rec.cib_cents == parse_cents(p.get("cib-price"))
    assert rec.new_cents == parse_cents(p.get("new-price"))


async def test_nothing_shown_at_startup(h):
    await h.ticks(5)
    assert not h.hub.visible and h.hub.snapshot()["record"] is None


async def test_presented_game_is_displayed(h):
    await h.present("900101")
    assert h.hub.visible
    rec = h.hub.current
    assert rec.product_id == "900101" and rec.platform == "NINTENDO 64"
    assert (rec.loose_cents, rec.cib_cents, rec.new_cents) == (4250, 10999, 59900)
    assert rec.demo is False and rec.source == "auto"
    assert h.engine.state == PipelineState.DISPLAYING
    assert_record_consistent(rec)


async def test_same_game_does_not_repeat_api_calls(h):
    await h.present("900101")
    calls_r, calls_pc = h.recognizer.calls, len(h.mock_pc.calls)
    for _ in range(4):  # operator wiggles the same game
        await h.present("900101")
    assert h.recognizer.calls == calls_r
    assert len(h.mock_pc.calls) == calls_pc


async def test_replacement_while_lookup_in_progress(h):
    h.recognizer.delay_for["900101"] = 0.5
    await h.present("900101", settle=False)
    assert h.engine._job is not None and not h.engine._job.done()
    await h.present("900302")  # different game arrives before the first finished
    await asyncio.sleep(0.6)
    await h.ticks(1)
    assert h.hub.current.product_id == "900302"
    assert [r.product_id for r in h.shown] == ["900302"], "superseded game must never be displayed"
    assert h.engine.jobs_superseded >= 1


async def test_out_of_order_price_responses_discarded(h):
    h.mock_pc.delay_s = lambda path, params: 0.6 if params.get("id") == "900101" else 0.0
    await h.present("900101", settle=False)
    for _ in range(40):  # wait until the first job is blocked on its price lookup
        if h.engine.state == PipelineState.PRICE_LOOKUP:
            break
        await asyncio.sleep(0.02)
    await h.present("900401")
    await asyncio.sleep(0.8)  # the slow, older response arrives now
    assert h.hub.current.product_id == "900401"
    assert all(r.product_id == "900401" for r in h.shown)


async def test_every_shown_record_is_consistent(h):
    for pid in ["900101", "900302", "900601", "900501", "900701", "900401"]:
        await h.present(pid)
    assert len(h.shown) == 6
    for rec in h.shown:
        assert_record_consistent(rec)
    earthbound = next(r for r in h.shown if r.product_id == "900501")
    assert earthbound.new_cents is None  # missing value -> N/A, never $0.00
    snap = earthbound.to_overlay_dict()
    assert snap["newUsd"] is None and snap["newCents"] is None


async def test_unidentified_item_hides_old_prices_after_grace(h):
    await h.present("900101")
    h.recognizer.overrides[None] = {"confidence": "low", "title": "Mystery Cart"}
    await h.present(None, item=unknown_item())
    assert h.hub.visible, "old record stays during the grace period"
    assert h.engine.state == PipelineState.NEEDS_CONFIRMATION
    h.clock.t += 3.5
    await h.ticks(1, advance=0)
    assert not h.hub.visible


async def test_empty_presentation_area_hides_after_grace(h):
    await h.present("900101")
    await h.present(None)  # recognizer reports nothing presented
    assert h.hub.visible
    h.clock.t += 4.5
    await h.ticks(1, advance=0)
    assert not h.hub.visible


async def test_original_game_returns_reuses_memory(h):
    await h.present("900101")
    await h.present("900302")
    calls_r, calls_pc = h.recognizer.calls, len(h.mock_pc.calls)
    await h.present("900101")
    assert h.hub.current.product_id == "900101"
    assert h.recognizer.calls == calls_r, "visual memory should skip Claude for a returning game"
    assert len(h.mock_pc.calls) == calls_pc, "cached prices are reused within the refresh window"


async def test_hold_prevents_automatic_replacement(h):
    await h.present("900101")
    h.engine.hold(True)
    await h.present("900302")
    assert h.hub.current.product_id == "900101"
    assert h.engine.pending_record.product_id == "900302"
    await h.present(None)
    h.clock.t += 30
    await h.ticks(2)
    assert h.hub.visible, "held game is not auto-hidden"
    assert await h.engine.show_pending() is True
    assert h.hub.current.product_id == "900302"


async def test_manual_correction_is_learned(h):
    h.recognizer.overrides["900301"] = {"confidence": "low"}
    await h.present("900301")
    assert not h.hub.visible and h.engine.state == PipelineState.NEEDS_CONFIRMATION
    sugg = h.engine.current.match["suggestions"]
    assert any(s["productId"] == "900301" for s in sugg)
    rec = await h.engine.select_product("900301", correction=True, generation=h.engine.current.generation)
    assert rec.source == "manual" and h.hub.current.product_id == "900301"
    # Next time the same (low confidence) recognition maps straight to the corrected product.
    h.engine.clear_overlay()
    h.engine.forget_visual_memory()
    await h.present("900301")
    assert h.hub.current.product_id == "900301"
    assert h.engine.current.match["via"] == "match_cache"


async def test_stale_prices_are_not_displayed(h):
    await h.present("900101")
    h.engine.clear_overlay()
    h.engine.forget_visual_memory()
    h.db.execute("UPDATE price_cache SET retrieved_at = retrieved_at - 7200")
    h.mock_pc.fail_next = [500] * 20
    await h.present("900101")
    assert not h.hub.visible
    assert h.engine.state == PipelineState.ERROR
    assert h.engine.current.stale_prices["productId"] == "900101"


async def test_stale_prices_labeled_when_policy_allows(db):
    harness = Harness(db, pricing={"stale_policy": "show_labeled"})
    try:
        await harness.present("900101")
        harness.engine.clear_overlay()
        harness.engine.forget_visual_memory()
        db.execute("UPDATE price_cache SET retrieved_at = retrieved_at - 7200")
        harness.mock_pc.fail_next = [500] * 20
        await harness.present("900101")
        assert harness.hub.current.freshness == "stale"
        assert harness.hub.snapshot()["record"]["freshness"] == "stale"
    finally:
        await harness.close()


async def test_corroboration_before_replacing_high_confidence_game(h):
    await h.present("900101")
    # medium confidence for the replacement; the second look disagrees -> no replacement
    h.recognizer.overrides["900302"] = {"confidence": "medium"}
    h.recognizer.sequence_overrides = [{}, {"platform": "Sega Genesis", "title": "Mortal Kombat"}]
    calls = h.recognizer.calls
    await h.present("900302")
    assert h.recognizer.calls == calls + 2
    assert h.hub.current.product_id == "900101"
    assert h.engine.state == PipelineState.NEEDS_CONFIRMATION


async def test_corroboration_agreeing_replaces(h):
    await h.present("900101")
    h.recognizer.overrides["900302"] = {"confidence": "medium"}
    await h.present("900302")
    assert h.hub.current.product_id == "900302"


async def test_blurry_image_never_calls_claude(h):
    h.camera.blur = 8.0
    await h.present("900101")
    assert h.recognizer.calls == 0
    assert "quality" in h.engine.current.status.lower()


async def test_lit_game_on_black_backdrop_is_recognized(h):
    """Operator report: a lit game on a black backdrop was rejected as 'too dark'."""
    h.camera.backdrop = (8, 8, 8)
    h.camera.item_scale = 0.75  # the game fills only part of the presentation area
    await h.present("900101")
    assert h.recognizer.calls >= 1, h.engine.current.status
    assert h.hub.current is not None and h.hub.current.product_id == "900101"
    assert h.engine.current.quality["subject"] < 0.5, "measured on the game, not the backdrop"


async def test_dim_scene_asks_for_more_light(h):
    h.camera.backdrop = (8, 8, 8)
    h.camera.exposure = 0.12
    await h.present("900101")
    assert h.recognizer.calls == 0
    assert "too dark" in h.engine.current.status and "more light" in h.engine.current.status


async def test_spend_limit_pauses_detection(h):
    h.recognizer.fail_status = "spend_limit"
    await h.present("900101")
    assert h.engine.detection_enabled is False
    assert h.engine.state == PipelineState.PAUSED
    assert not h.hub.visible


async def test_obs_disconnect_keeps_current_record(h):
    await h.present("900101")
    h.camera.connected = False
    await h.ticks(5)
    assert h.hub.visible and h.hub.current.product_id == "900101"
    assert h.engine.state == PipelineState.IDLE
    h.camera.connected = True
    await h.present("900302")
    assert h.hub.current.product_id == "900302"


async def test_clear_cancels_inflight_job(h):
    h.recognizer.delay_for["900101"] = 0.4
    await h.present("900101", settle=False)
    h.engine.clear_overlay()
    await asyncio.sleep(0.6)
    assert not h.hub.visible and h.shown == []


async def test_refresh_prices_applies_only_to_same_product(h):
    await h.present("900101")
    h.mock_pc.catalog["900101"]["loose-price"] = 4999
    rec = await h.engine.refresh_prices()
    assert rec.loose_cents == 4999 and h.hub.current.loose_cents == 4999
    assert_record_consistent  # prices changed upstream; record reflects the API exactly


async def test_recall_from_history(h):
    await h.present("900101")
    await h.present("900302")
    hist = h.engine.history()
    zelda = next(x for x in hist if x["productId"] == "900101")
    rec = await h.engine.recall(zelda["id"])
    assert rec.product_id == "900101" and rec.source == "recall"


@pytest.mark.slow
async def test_long_session_200_games_bounded_resources(db):
    harness = Harness(db)
    try:
        order = list(ITEMS)  # 10 demo games, some re-presented many times
        await harness.present(order[0])
        gc.collect()
        tracemalloc.start()
        snap0 = tracemalloc.take_snapshot()
        tasks0 = len(asyncio.all_tasks())
        for i in range(200):
            pid = order[i % len(order)]
            if i % 7 == 3:
                harness.engine.forget_visual_memory()  # force full recognition path regularly
            await harness.present(pid)
            assert harness.hub.current.product_id == pid
        gc.collect()
        snap1 = tracemalloc.take_snapshot()
        tracemalloc.stop()
        growth = sum(s.size_diff for s in snap1.compare_to(snap0, "filename"))
        assert growth < 8_000_000, f"memory grew by {growth} bytes"
        assert len(asyncio.all_tasks()) <= tasks0 + 2, "background tasks must not accumulate"
        assert len(harness.engine._memory) <= 400
        assert len(harness.shown) >= 200
        # PriceCharting traffic: one product fetch per distinct game, searches cached.
        product_calls = [c for c in harness.mock_pc.calls if c[0] == "/api/product"]
        assert len(product_calls) == len(order)
        assert all(r.product_id in CATALOG for r in harness.shown)
        json.dumps(harness.engine.status(), default=str)
    finally:
        await harness.close()


# ---------------------------------------------------------------- regression tests (code review)

async def test_returning_to_on_air_game_cancels_other_items_job(h):
    """Review #1: A on air, B starts a job, A comes back before B finishes -> B never shown."""
    await h.present("900101")
    h.recognizer.delay_for["900302"] = 0.5
    await h.present("900302", settle=False)
    assert h.engine._job is not None and not h.engine._job.done()
    await h.present("900101", settle=False)  # back to the on-air game while B is in flight
    await asyncio.sleep(0.8)
    await h.ticks(4)
    assert h.hub.current.product_id == "900101"
    assert [r.product_id for r in h.shown] == ["900101"]


async def test_plain_manual_pick_does_not_rebind_recognition(h):
    """Review #2: showing a product for a buyer is not a correction."""
    await h.present("900101")
    await h.engine.select_product("900302")  # no correction flag
    h.engine.clear_overlay()
    h.engine.forget_visual_memory()
    await h.present("900101")
    assert h.hub.current.product_id == "900101"


async def test_stale_correction_generation_is_not_learned(h):
    h.recognizer.overrides["900301"] = {"confidence": "low"}
    await h.present("900301")
    stale_gen = h.engine.current.generation - 1
    await h.engine.select_product("900302", correction=True, generation=stale_gen)
    h.engine.clear_overlay()
    h.engine.forget_visual_memory()
    await h.present("900301")
    assert h.engine.state == PipelineState.NEEDS_CONFIRMATION, "stale correction must not be remembered"


async def test_recall_does_not_poison_visual_memory(h):
    """Review #3: recalling another game while an ambiguous item is on camera."""
    await h.present("900401")
    hist = h.engine.history()
    h.recognizer.overrides["900301"] = {"confidence": "low"}
    await h.present("900301")
    assert h.engine.state == PipelineState.NEEDS_CONFIRMATION
    await h.engine.recall(hist[0]["id"])  # Resident Evil, not the item on camera
    h.engine.clear_overlay()
    calls = h.recognizer.calls
    await h.present("900301")
    assert h.recognizer.calls == calls + 1, "must re-identify, not reuse the recalled product"
    assert h.engine.state == PipelineState.NEEDS_CONFIRMATION


async def test_releasing_hold_shows_pending_with_price_check(h):
    """Review #5: pending record goes through the freshness policy when shown."""
    await h.present("900101")
    h.engine.hold(True)
    await h.present("900302")
    assert h.engine.pending_record.product_id == "900302"
    h.db.execute("UPDATE price_cache SET retrieved_at = retrieved_at - 7200 WHERE product_id='900302'")
    h.mock_pc.fail_next = [500] * 20
    from gameoverlay.pricing.service import PriceUnavailable

    with pytest.raises(PriceUnavailable):
        await h.engine.show_pending()
    assert h.hub.current.product_id == "900101", "stale pending prices must not go on air"


async def test_on_air_prices_are_revalidated_when_they_expire(h):
    await h.present("900101")
    import time as _t

    h.engine._wall = lambda: _t.time() + 7200  # an hour+ later
    h.db.execute("UPDATE price_cache SET retrieved_at = retrieved_at - 7200")
    h.mock_pc.fail_next = [500] * 20
    await h.ticks(1)
    await asyncio.wait_for(h.engine._age_task, 5)
    assert not h.hub.visible, "expired prices that cannot be refreshed are taken off air"


async def test_on_air_prices_refresh_in_place(h):
    await h.present("900101")
    import time as _t

    h.engine._wall = lambda: _t.time() + 7200
    h.db.execute("UPDATE price_cache SET retrieved_at = retrieved_at - 7200")
    h.mock_pc.catalog["900101"]["loose-price"] = 4999
    await h.ticks(1)
    await asyncio.wait_for(h.engine._age_task, 5)
    assert h.hub.current.product_id == "900101" and h.hub.current.loose_cents == 4999
