"""PriceCharting client: field mapping, auth failure (8), rate limiting (9), missing values (10)."""

from __future__ import annotations

import asyncio

import pytest

from gameoverlay.demo.mock_pricecharting import DEMO_TOKEN, MockPriceCharting
from gameoverlay.pricing.client import (
    PriceChartingAuthError,
    PriceChartingBadResponse,
    PriceChartingClient,
    PriceChartingRateLimited,
    PriceChartingUnavailable,
)
from gameoverlay.pricing.ratelimit import SerialRateLimiter


async def test_product_maps_fields_exactly(pc_client):
    p = await pc_client.product("900101")
    assert p.product_name == "Zelda Ocarina of Time"
    assert p.console_name == "Nintendo 64"
    assert (p.loose_cents, p.cib_cents, p.new_cents) == (4250, 10999, 59900)


async def test_ignores_retail_graded_and_box_prices(pc_client, mock_pc):
    mock_pc.catalog["900501"]  # EarthBound has no new-price but has graded/retail fields
    p = await pc_client.product("900501")
    assert p.new_cents is None, "graded/retail values must never be substituted for NEW"
    assert p.loose_cents == 17244 and p.cib_cents == 42995


async def test_missing_values_are_none(pc_client, mock_pc):
    mock_pc.catalog["900101"] = {
        "status": "success",
        "id": "900101",
        "product-name": "Zelda Ocarina of Time",
        "console-name": "Nintendo 64",
        "cib-price": 0,
        "new-price": None,
    }
    p = await pc_client.product("900101")
    assert (p.loose_cents, p.cib_cents, p.new_cents) == (None, None, None)


async def test_search_returns_candidates(pc_client):
    res = await pc_client.search("mortal kombat")
    consoles = {c.console_name for c in res if c.product_name == "Mortal Kombat"}
    assert {"Sega Genesis", "Super Nintendo", "GameBoy", "Sega CD"} <= consoles


async def test_malformed_search_entries_are_skipped(pc_client, mock_pc, monkeypatch):
    def bad_search(q):
        return [{"id": "x1"}, "junk", {"id": "900101", "product-name": "Zelda", "console-name": "Nintendo 64"}]

    monkeypatch.setattr(mock_pc, "_search", bad_search)
    res = await pc_client.search("zelda")
    assert [c.product_id for c in res] == ["900101"]


async def test_wrong_product_id_in_response_rejected(pc_client, mock_pc):
    mock_pc.catalog["900102"] = dict(mock_pc.catalog["900101"])  # returns id 900101 for 900102
    with pytest.raises(PriceChartingBadResponse):
        await pc_client.product("900102")


async def test_auth_failure_401_not_retried(mock_pc, fake_clock):
    limiter = SerialRateLimiter(1.0, clock=fake_clock, sleep=fake_clock.sleep)
    client = PriceChartingClient(lambda: "wrong-token", limiter, transport=mock_pc.transport(), sleep=fake_clock.sleep)
    with pytest.raises(PriceChartingAuthError) as ei:
        await client.product("900101")
    assert "wrong-token" not in str(ei.value)
    assert len(mock_pc.calls) == 1
    assert client.status()["state"] == "auth_failed"
    await client.aclose()


async def test_auth_failure_error_message(pc_client, mock_pc):
    mock_pc.fail_next = ["autherr"]
    with pytest.raises(PriceChartingAuthError):
        await pc_client.product("900101")


async def test_missing_token():
    limiter = SerialRateLimiter(1.0)
    client = PriceChartingClient(lambda: None, limiter, transport=MockPriceCharting().transport())
    with pytest.raises(PriceChartingAuthError, match="not configured"):
        await client.search("zelda")
    assert client.status()["state"] == "not_configured"
    await client.aclose()


async def test_429_backs_off_and_retries(pc_client, mock_pc, fake_clock):
    mock_pc.fail_next = [429, 429]
    t0 = fake_clock()
    p = await pc_client.product("900101")
    assert p.product_id == "900101"
    assert len(mock_pc.calls) == 3
    assert fake_clock() - t0 >= 2.0  # honoured Retry-After
    assert pc_client.rate_limited_total == 2


async def test_429_gives_up_after_retries(pc_client, mock_pc):
    mock_pc.fail_next = [429] * 10
    with pytest.raises(PriceChartingRateLimited):
        await pc_client.product("900101")
    assert len(mock_pc.calls) == pc_client.max_retries + 1


async def test_5xx_and_timeouts_retry_then_succeed(pc_client, mock_pc):
    mock_pc.fail_next = [503, "timeout", "badjson"]
    p = await pc_client.product("900101")
    assert p.product_id == "900101"
    assert len(mock_pc.calls) == 4


async def test_persistent_outage_raises_unavailable(pc_client, mock_pc):
    mock_pc.fail_next = [500] * 10
    with pytest.raises(PriceChartingUnavailable):
        await pc_client.product("900101")


async def test_never_exceeds_one_request_per_second(mock_pc, fake_clock):
    """Concurrent callers are serialized and spaced >= 1 s apart (even if misconfigured)."""
    limiter = SerialRateLimiter(0.2, clock=fake_clock, sleep=fake_clock.sleep)  # clamps to 1.05
    assert limiter.interval == 1.05
    mock_pc.clock = fake_clock
    client = PriceChartingClient(lambda: DEMO_TOKEN, limiter, transport=mock_pc.transport(), sleep=fake_clock.sleep)
    await asyncio.gather(*(client.product(pid) for pid in ["900101", "900102", "900103", "900201", "900202"] * 3))
    times = mock_pc.call_times
    assert len(times) == 15
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 1.0 - 1e-9
    await client.aclose()


async def test_token_never_in_logs(pc_client, mock_pc, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    # Even if someone turns httpx logging all the way up, the token must not appear.
    caplog.set_level(logging.DEBUG, logger="httpx")
    mock_pc.fail_next = [500] * 10
    with pytest.raises(PriceChartingUnavailable):
        await pc_client.product("900101")
    assert DEMO_TOKEN not in caplog.text
