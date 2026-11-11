"""Deterministic harness for driving the pipeline engine in tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from gameoverlay.config import RuntimeSettings
from gameoverlay.demo.mock_pricecharting import DEMO_TOKEN, MockPriceCharting
from gameoverlay.demo.mock_vision import identification_for
from gameoverlay.demo.scene import DEMO_ITEMS, DemoItem, render_scene, to_jpeg
from gameoverlay.overlay.hub import OverlayHub
from gameoverlay.pipeline.engine import Engine
from gameoverlay.pipeline.matcher import Matcher
from gameoverlay.pricing.client import PriceChartingClient
from gameoverlay.pricing.ratelimit import SerialRateLimiter
from gameoverlay.pricing.service import PricingService
from gameoverlay.vision.recognizer import RecognitionOutcome

ITEMS = {i.product_id: i for i in DEMO_ITEMS}


class FakeCamera:
    def __init__(self) -> None:
        self.item: DemoItem | None = None
        self.connected = True
        self.blur = 0.0
        self.backdrop = None  # plain backdrop colour (None = wood table)
        self.exposure = 1.0
        self.item_scale = 1.0
        self.jitter = 0.0
        self.t = 0.0
        self.shots = 0
        self.state = "connected"

    async def screenshot(self, source, width=None, quality=90) -> bytes:
        self.shots += 1
        self.t += 0.45 if self.jitter else 0.1  # hand motion between samples is large
        if not self.connected:
            from gameoverlay.obs.client import OBSNotConnected

            raise OBSNotConnected("down")
        img = render_scene(self.item, t=self.t, jitter=self.jitter, blur=self.blur, size=(640, 360),
                           backdrop=self.backdrop, exposure=self.exposure, item_scale=self.item_scale)
        return to_jpeg(img, width=min(width or 640, 640), quality=quality)

    def on_state(self, cb):
        pass

    def status(self) -> dict:
        return {"state": "connected" if self.connected else "disconnected"}


class WorldRecognizer:
    """Behaves like Claude looking at the FakeCamera; configurable per-item."""

    def __init__(self, camera: FakeCamera) -> None:
        self.camera = camera
        self.calls = 0
        self.delay_for: dict[str | None, float] = {}
        self.overrides: dict[str | None, dict] = {}
        self.fail_status: str | None = None
        self.sequence_overrides: list[dict] = []

    async def recognize(self, images):
        self.calls += 1
        item = self.camera.item
        pid = item.product_id if item else None
        await asyncio.sleep(self.delay_for.get(pid, 0.0))
        if self.fail_status:
            return RecognitionOutcome(self.fail_status, error=self.fail_status)
        ov = dict(self.overrides.get(pid, {}))
        if self.sequence_overrides:
            ov.update(self.sequence_overrides.pop(0))
        return RecognitionOutcome("ok", result=identification_for(item, **ov), model="test", images=len(images))

    def status(self):
        return {"state": "test", "usage": {}}


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Harness:
    def __init__(self, db, **settings_patch) -> None:
        base = {
            "obs": {"camera_source": "Sony ZV-1", "still_max_width": 640},
            "detection": {"stable_frames_required": 2, "burst_frames": 1, "burst_spacing_ms": 50, "min_sharpness": 20},
            "display": {"unidentified_grace_s": 3.0, "empty_grace_s": 4.0, "min_display_s": 0.0},
        }
        from gameoverlay.config import _deep_merge

        _deep_merge(base, settings_patch)
        self.settings = RuntimeSettings().merged(base)
        self.db = db
        self.clock = FakeClock()
        self.camera = FakeCamera()
        self.recognizer = WorldRecognizer(self.camera)
        self.mock_pc = MockPriceCharting()

        limiter_t = [0.0]

        async def no_sleep(s):
            limiter_t[0] += max(0.0, s)  # fake time advances instantly
            await asyncio.sleep(0)

        self.limiter = SerialRateLimiter(1.0, clock=lambda: limiter_t[0], sleep=no_sleep)
        self.pc = PriceChartingClient(lambda: DEMO_TOKEN, self.limiter, transport=self.mock_pc.transport(), sleep=no_sleep)
        self.pricing = PricingService(self.pc, db, lambda: self.settings.pricing)
        self.matcher = Matcher(self.pricing, db, lambda: self.settings.recognition)
        self.hub = OverlayHub(False, lambda: self.settings.overlay, lambda: self.settings.display)
        self.shown: list = []
        orig_show = self.hub.show

        def spy_show(rec):
            self.shown.append(rec)
            return orig_show(rec)

        self.hub.show = spy_show
        self.engine = Engine(
            obs=self.camera, recognizer=self.recognizer, matcher=self.matcher, pricing=self.pricing,
            hub=self.hub, db=db, settings=lambda: self.settings, session_id="test-session", clock=self.clock,
        )

    async def ticks(self, n: int = 1, advance: float = 0.4) -> None:
        for _ in range(n):
            self.clock.t += advance
            await self.engine._tick()
            await asyncio.sleep(0)

    async def settle(self, timeout: float = 5.0) -> None:
        """Let the current job finish."""
        job = self.engine._job
        if job is not None:
            try:
                await asyncio.wait_for(asyncio.shield(job), timeout)
            except (asyncio.CancelledError, Exception):
                pass

    async def present(self, product_id: str | None, settle: bool = True, item: DemoItem | None = None) -> None:
        """Hand-motion, then hold the item steady until stable."""
        self.camera.jitter = 1.0
        self.camera.item = item if item is not None else (ITEMS.get(product_id) if product_id else None)
        await self.ticks(2)
        self.camera.jitter = 0.0
        await self.ticks(3)
        if settle:
            await self.settle()
            await self.ticks(1)

    async def close(self) -> None:
        await self.engine.stop()
        await self.pc.aclose()


def unknown_item(title="MYSTERY CART", platform="SEGA GENESIS") -> DemoItem:
    return replace(DEMO_ITEMS[0], product_id=None, label_title=title, label_platform=platform,
                   platform="Sega Genesis", color=(10, 200, 200))
