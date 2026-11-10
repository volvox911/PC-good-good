from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Make sure tests never read real credentials from the developer's environment/keyring.
# (Live tests, which are opt-in, read the saved originals from LIVE_ENV.)
LIVE_ENV = {k: os.environ.get(k) for k in (
    "ANTHROPIC_API_KEY", "PRICECHARTING_TOKEN", "PRICECHARTING_API_TOKEN", "OBS_PASSWORD",
    "OBS_WEBSOCKET_PASSWORD", "WGO_LIVE_OBS_HOST", "WGO_LIVE_OBS_PORT", "WGO_LIVE_OBS_SOURCE")}
for var in (
    "ANTHROPIC_API_KEY",
    "PRICECHARTING_TOKEN",
    "PRICECHARTING_API_TOKEN",
    "OBS_PASSWORD",
    "OBS_WEBSOCKET_PASSWORD",
):
    os.environ.pop(var, None)
os.environ["WGO_DISABLE_KEYRING"] = "true"
os.environ["WGO_ENV"] = "test"

from gameoverlay.config import PricingSettings  # noqa: E402
from gameoverlay.db import Database  # noqa: E402
from gameoverlay.demo.mock_pricecharting import DEMO_TOKEN, MockPriceCharting  # noqa: E402
from gameoverlay.pricing.client import PriceChartingClient  # noqa: E402
from gameoverlay.pricing.ratelimit import SerialRateLimiter  # noqa: E402
from gameoverlay.pricing.service import PricingService  # noqa: E402


class FakeClock:
    """Monotonic fake clock whose sleep() advances time instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += max(0.0, s)
        await asyncio.sleep(0)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "t.sqlite3")
    yield d
    d.close()


@pytest.fixture
def mock_pc() -> MockPriceCharting:
    return MockPriceCharting()


@pytest.fixture
def pricing_settings() -> PricingSettings:
    return PricingSettings()


@pytest.fixture
async def pc_client(mock_pc, fake_clock):
    limiter = SerialRateLimiter(1.2, clock=fake_clock, sleep=fake_clock.sleep)
    mock_pc.clock = fake_clock
    client = PriceChartingClient(
        lambda: DEMO_TOKEN, limiter, transport=mock_pc.transport(), sleep=fake_clock.sleep
    )
    yield client
    await client.aclose()


@pytest.fixture
async def pricing(pc_client, db, pricing_settings):
    return PricingService(pc_client, db, lambda: pricing_settings)
