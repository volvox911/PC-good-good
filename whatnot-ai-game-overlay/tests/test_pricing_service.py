"""Cache expiration (12) and safe handling of stale pricing (18)."""

from __future__ import annotations

import pytest

from gameoverlay.config import PricingSettings
from gameoverlay.pricing.service import PriceUnavailable, PricingService


class WallClock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def wall():
    return WallClock()


@pytest.fixture
def svc(pc_client, db, wall):
    settings = PricingSettings(price_refresh_s=3600, max_stale_s=86400)
    s = PricingService(pc_client, db, lambda: settings, clock=wall)
    s._cfg = settings
    return s


async def _prime(svc, wall, monkeypatch, pid="900101"):
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: wall.t)
    return await svc.get_prices(pid)


async def test_first_lookup_fresh_then_cached(svc, mock_pc, wall, monkeypatch):
    first = await _prime(svc, wall, monkeypatch)
    assert first.freshness == "fresh" and first.refreshed
    n = len(mock_pc.calls)
    second = await svc.get_prices("900101")
    assert second.freshness == "cached" and not second.refreshed
    assert len(mock_pc.calls) == n, "cache hit must not call the API"


async def test_cache_expires_after_refresh_interval(svc, mock_pc, wall, monkeypatch):
    await _prime(svc, wall, monkeypatch)
    n = len(mock_pc.calls)
    wall.t += 3601
    again = await svc.get_prices("900101")
    assert again.freshness == "fresh"
    assert len(mock_pc.calls) == n + 1


async def test_force_refresh(svc, mock_pc, wall, monkeypatch):
    await _prime(svc, wall, monkeypatch)
    n = len(mock_pc.calls)
    r = await svc.get_prices("900101", force_refresh=True)
    assert r.refreshed and len(mock_pc.calls) == n + 1


async def test_stale_hidden_when_refresh_fails(svc, mock_pc, wall, monkeypatch):
    await _prime(svc, wall, monkeypatch)
    wall.t += 7200
    mock_pc.fail_next = [500] * 10
    with pytest.raises(PriceUnavailable) as ei:
        await svc.get_prices("900101")
    assert ei.value.stale is not None  # dashboard can show it, labeled stale


async def test_stale_shown_labeled_when_policy_allows(svc, mock_pc, wall, monkeypatch):
    await _prime(svc, wall, monkeypatch)
    svc._cfg.stale_policy = "show_labeled"
    wall.t += 7200
    mock_pc.fail_next = [500] * 10
    r = await svc.get_prices("900101")
    assert r.freshness == "stale"


async def test_too_stale_never_shown(svc, mock_pc, wall, monkeypatch):
    await _prime(svc, wall, monkeypatch)
    svc._cfg.stale_policy = "show_labeled"
    wall.t += 90000  # > max_stale_s
    mock_pc.fail_next = [500] * 10
    with pytest.raises(PriceUnavailable):
        await svc.get_prices("900101")


async def test_concurrent_lookups_deduplicated(svc, mock_pc, wall, monkeypatch):
    import asyncio
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: wall.t)
    results = await asyncio.gather(*(svc.get_prices("900201") for _ in range(5)))
    assert all(r.prices.product_id == "900201" for r in results)
    assert sum(1 for p, _ in mock_pc.calls if p == "/api/product") == 1


async def test_search_cache(svc, mock_pc):
    a = await svc.search("Mortal Kombat")
    n = len(mock_pc.calls)
    b = await svc.search("mortal   kombat")
    assert a == b and len(mock_pc.calls) == n


async def test_local_suggestions_need_no_api(svc, mock_pc, wall, monkeypatch):
    await _prime(svc, wall, monkeypatch, "900401")
    n = len(mock_pc.calls)
    sug = svc.local_suggestions("resident")
    assert [c.product_id for c in sug] == ["900401"]
    assert len(mock_pc.calls) == n


async def test_cancelling_one_caller_does_not_fail_the_others(svc, mock_pc, wall, monkeypatch):
    """Review #6: a superseded job's cancellation must not break a concurrent manual pick."""
    import asyncio
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: wall.t)
    mock_pc.delay_s = 0.2
    first = asyncio.create_task(svc.get_prices("900302"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(svc.get_prices("900302"))
    await asyncio.sleep(0.01)
    first.cancel()
    r = await second
    assert r.prices.product_id == "900302"
    assert svc.cached_prices("900302") is not None
