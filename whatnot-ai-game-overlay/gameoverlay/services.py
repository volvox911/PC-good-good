"""Application service container: builds and wires every component.

Live mode uses the real OBS WebSocket, Claude Vision and PriceCharting API.
Demo mode swaps in a fake OBS server (still driven through the real OBS client), a
mock recognizer and a mock PriceCharting transport (still driven through the real
rate-limited client) — and uses a *separate database* so demo data can never leak
into live caches or history.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .config import AppConfig, Environment, RuntimeSettings, migrate_settings
from .db import Database
from .logging_setup import set_level
from .overlay.hub import OverlayHub
from .pipeline.engine import Engine
from .pipeline.matcher import Matcher
from .pricing.client import PriceChartingClient
from .pricing.ratelimit import SerialRateLimiter
from .pricing.service import PricingService
from .obs.client import OBSClient
from .secrets_store import SecretStore, load_or_create_admin_token
from .vision.recognizer import ClaudeRecognizer
from .vision.usage import UsageTracker

log = logging.getLogger(__name__)

SETTINGS_KEY = "runtime"


class Services:
    def __init__(self, config: AppConfig, *, pc_transport=None, recognizer=None, obs_client=None) -> None:
        self.config = config
        self.demo = config.demo
        self.started_at = time.time()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        db_path = config.data_dir / "demo.sqlite3" if self.demo else config.db_path
        self.db = Database(db_path)
        self.session_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.recovered_from_crash = self.db.start_session(self.session_id)
        self.db.prune()
        self.secrets = SecretStore(config.data_dir, use_keyring=not config.disable_keyring and config.env != Environment.TEST)
        self.admin_token = load_or_create_admin_token(config.data_dir)
        self.settings = self._load_settings()

        # Demo infrastructure ---------------------------------------------------------
        self.demo_world = None
        self.fake_obs = None
        self._demo_obs_port: int | None = None
        if self.demo:
            from .demo.mock_pricecharting import DEMO_TOKEN, MockPriceCharting
            from .demo.mock_vision import MockRecognizer
            from .demo.world import DemoWorld

            self.demo_world = DemoWorld()
            self.mock_pc = MockPriceCharting()
            pc_transport = pc_transport or self.mock_pc.transport()
            token_provider = lambda: DEMO_TOKEN  # noqa: E731
            recognizer = recognizer or MockRecognizer(self.demo_world.current_item)
        else:
            token_provider = lambda: self.secrets.get("pricecharting_token")  # noqa: E731

        # PriceCharting --------------------------------------------------------------------
        p = self.settings.pricing
        self.limiter = SerialRateLimiter(p.min_request_interval_s)
        self.pc_client = PriceChartingClient(
            token_provider, self.limiter, timeout_s=p.request_timeout_s, max_retries=p.max_retries, transport=pc_transport
        )
        self.pricing = PricingService(self.pc_client, self.db, lambda: self.settings.pricing)
        self.matcher = Matcher(self.pricing, self.db, lambda: self.settings.recognition)

        # Claude -----------------------------------------------------------------------------
        self.usage = UsageTracker(self.db, self.session_id, lambda: self.settings.recognition)
        self.recognizer = recognizer or ClaudeRecognizer(
            lambda: self.secrets.get("anthropic_api_key"), lambda: self.settings.recognition, self.usage
        )

        # OBS ----------------------------------------------------------------------------------
        self.obs = obs_client or OBSClient(
            self._obs_address,
            (lambda: None) if self.demo else (lambda: self.secrets.get("obs_password")),
            lambda: self.settings.obs.request_timeout_s,
        )

        # Overlay + engine -------------------------------------------------------------------
        self.hub = OverlayHub(self.demo, lambda: self.settings.overlay, lambda: self.settings.display)
        self.engine = Engine(
            obs=self.obs,
            recognizer=self.recognizer,
            matcher=self.matcher,
            pricing=self.pricing,
            hub=self.hub,
            db=self.db,
            settings=lambda: self.settings,
            session_id=self.session_id,
            demo=self.demo,
        )
        self._listeners: list = []
        self.hub.on_change(self._notify)
        self.engine.on_change(self._notify)
        self.obs.on_state(lambda _s: self._notify())

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> RuntimeSettings:
        raw = self.db.get_setting(SETTINGS_KEY)
        try:
            if raw:
                migrated = migrate_settings(raw)
                s = RuntimeSettings.model_validate(migrated)
                if migrated != raw:
                    self.db.set_setting(SETTINGS_KEY, s.model_dump())
            else:
                s = RuntimeSettings()
        except Exception:
            log.error("stored settings were invalid; using defaults")
            s = RuntimeSettings()
        if self.demo:
            s = s.merged({"obs": {"camera_source": s.obs.camera_source or "Sony ZV-1"}, "setup_completed": True})
        return s

    def update_settings(self, patch: dict) -> RuntimeSettings:
        old = self.settings
        new = old.merged(patch)
        self.settings = new
        self.db.set_setting(SETTINGS_KEY, new.model_dump())
        # live side effects
        set_level(new.log_level)
        self.limiter.set_interval(new.pricing.min_request_interval_s)
        self.pc_client.max_retries = new.pricing.max_retries
        if (old.obs.host, old.obs.port) != (new.obs.host, new.obs.port) and not self.demo:
            self.obs.reconnect_now()
        if (old.obs.camera_source, old.obs.crop, old.obs.rotation) != (new.obs.camera_source, new.obs.crop, new.obs.rotation):
            self.engine.detector.rearm()
        if old.overlay != new.overlay or old.display.overlay_disconnect_hide_s != new.display.overlay_disconnect_hide_s:
            self.hub.layout_changed()
        self._notify()
        return new

    def _obs_address(self) -> tuple[str, int]:
        if self.demo and self._demo_obs_port:
            return "127.0.0.1", self._demo_obs_port
        return self.settings.obs.host, self.settings.obs.port

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.demo:
            from .demo.fake_obs import FakeOBSServer

            self.fake_obs = FakeOBSServer(self.demo_world.frame, password=None)
            self._demo_obs_port = await self.fake_obs.start()
            log.info("DEMO MODE: fake OBS camera on 127.0.0.1:%s, mock Claude, mock PriceCharting", self._demo_obs_port)
        self.hub.start()
        self.obs.start()
        self.engine.start()
        if self.recovered_from_crash:
            log.warning("previous session did not shut down cleanly; settings and caches were recovered")

    async def stop(self) -> None:
        # Record the clean shutdown first: when the console window is closed, Windows only
        # gives the process a few seconds, and the rest of the teardown is best-effort.
        try:
            self.db.end_session(self.session_id)
        except Exception:  # pragma: no cover
            log.exception("could not record clean shutdown")
        for step in (self.engine.stop, self.obs.stop, self.hub.stop):
            try:
                await asyncio.wait_for(step(), 3)
            except Exception:
                log.warning("shutdown step %s did not finish cleanly", getattr(step, "__qualname__", step))
        if self.fake_obs:
            await self.fake_obs.stop()
        await self.pc_client.aclose()
        self.db.close()

    # ------------------------------------------------------------------ change notification
    def on_change(self, cb) -> None:
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # pragma: no cover
                log.exception("services listener failed")

    # ------------------------------------------------------------------ dashboard snapshot
    def status(self) -> dict:
        s = self.settings
        rec = self.hub.current
        return {
            "mode": "demo" if self.demo else "live",
            "env": self.config.env.value,
            "version": __import__("gameoverlay").__version__,
            "session": {
                "id": self.session_id,
                "startedAt": self.started_at,
                "recoveredFromCrash": self.recovered_from_crash,
            },
            "obs": {**self.obs.status(), "cameraSource": s.obs.camera_source},
            "anthropic": self.recognizer.status(),
            "pricecharting": self.pc_client.status(),
            "secrets": {"backend": self.secrets.backend, **self.secrets.all_status()},
            "engine": self.engine.status(),
            "overlay": {
                "visible": self.hub.visible,
                "record": rec.to_overlay_dict() if rec else None,
                "source": rec.source if rec else None,
                "hiddenReason": self.hub.hidden_reason,
                "clients": self.hub.client_count,
                "seq": self.hub.seq,
                "shownAt": self.hub.shown_at,
                "url": f"{self.config.base_url}/overlay",
                "showPrices": s.overlay.show_prices,
            },
            "setupCompleted": s.setup_completed,
        }


async def wait_until(pred, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Small helper used by tests and the CLI self-check."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()
