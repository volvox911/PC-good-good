"""Full demo pipeline: fake OBS (real WebSocket client) -> mock vision -> mock PriceCharting
(real rate-limited client) -> overlay WebSocket. Demo data must be flagged/watermarked."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from gameoverlay.config import AppConfig, Environment
from gameoverlay.server import create_app
from gameoverlay.services import Services


def test_demo_mode_end_to_end(tmp_path):
    cfg = AppConfig(env=Environment.TEST, data_dir=tmp_path, port=8765, demo=True)
    svc = Services(cfg)
    svc.demo_world.paused_item = svc.demo_world.items[0]  # hold Zelda steady in front of the camera
    svc.update_settings({"detection": {"sample_interval_ms": 150, "stable_frames_required": 2, "burst_frames": 1}})
    svc.recognizer.latency_s = 0.05
    app = create_app(cfg, svc)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        with client.websocket_connect("/ws/overlay") as ws:
            first = ws.receive_json()
            assert first["demoMode"] is True
            deadline = time.time() + 20
            msg = first
            while not msg.get("visible") and time.time() < deadline:
                msg = ws.receive_json()
            assert msg["visible"], "demo pipeline never displayed a record"
            rec = msg["record"]
            assert rec["demo"] is True, "demo records must be flagged so the overlay watermarks them"
            assert rec["productId"] == "900101"
            assert svc.fake_obs.requests_seen and set(svc.fake_obs.requests_seen) <= {
                "GetVersion", "GetInputList", "GetSourceScreenshot", "GetSourceActive"}
    # demo used its own database file; the live database was never created
    assert (tmp_path / "demo.sqlite3").exists()
    assert not (tmp_path / "test.sqlite3").exists()
