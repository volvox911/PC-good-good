"""Camera preview endpoint: actionable errors for the setup wizard / crop editor."""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gameoverlay.config import AppConfig, Environment
from gameoverlay.demo.fake_obs import FakeOBSServer
from gameoverlay.obs.client import OBSClient
from gameoverlay.server import create_app
from gameoverlay.services import Services


def jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def setup(tmp_path):
    frame = {"color": (180, 120, 60)}
    server = FakeOBSServer(lambda name, w, q: jpeg(frame["color"]), password=None)
    holder = {}

    def make(with_obs=True):
        cfg = AppConfig(env=Environment.TEST, data_dir=tmp_path, port=8765)
        obs = None
        if with_obs:
            obs = OBSClient(lambda: ("127.0.0.1", holder["port"]), lambda: None)
        svc = Services(cfg, obs_client=obs)
        app = create_app(cfg, svc)
        return svc, app

    return server, holder, frame, make


def auth(svc):
    return {"Authorization": f"Bearer {svc.admin_token}"}


async def _start(server, holder):
    holder["port"] = await server.start()


def wait_connected(svc, timeout=5):
    end = time.time() + timeout
    while time.time() < end and not svc.obs.connected:
        time.sleep(0.05)
    return svc.obs.connected


def test_preview_needs_a_source(setup):
    _, _, _, make = setup
    svc, app = make(with_obs=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.get("/api/camera/preview.jpg", headers=auth(svc))
        assert r.status_code == 409 and "camera source" in r.json()["detail"].lower()


def test_preview_reports_obs_not_connected(setup):
    _, _, _, make = setup
    svc, app = make(with_obs=False)
    svc.update_settings({"obs": {"camera_source": "Video Capture Device 6", "port": 1}})
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.get("/api/camera/preview.jpg", headers=auth(svc))
        assert r.status_code == 503 and "not connected" in r.json()["detail"]


async def test_preview_errors_and_black_frame(setup):
    server, holder, frame, make = setup
    await _start(server, holder)
    try:
        svc, app = make()
        import asyncio

        def run():
            with TestClient(app, base_url="http://127.0.0.1:8765") as c:
                assert wait_connected(svc)
                # 1) source name OBS doesn't know -> 502 with a concrete hint
                svc.update_settings({"obs": {"camera_source": "Video Capture Device 6"}})
                r = c.get("/api/camera/preview.jpg", headers=auth(svc))
                assert r.status_code == 502
                d = r.json()["detail"]
                assert "Video Capture Device 6" in d and "Deactivate when not showing" in d
                # 2) working camera -> image, no warning
                svc.update_settings({"obs": {"camera_source": "Sony ZV-1"}})
                r = c.get("/api/camera/preview.jpg", headers=auth(svc))
                assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
                assert "x-preview-warning" not in r.headers
                # 3) camera delivering black frames (deactivated device) -> image + warning
                frame["color"] = (0, 0, 0)
                r = c.get("/api/camera/preview.jpg", headers=auth(svc))
                assert r.status_code == 200 and "black" in r.headers["x-preview-warning"]

        await asyncio.get_running_loop().run_in_executor(None, run)
    finally:
        await server.stop()
