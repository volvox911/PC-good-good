"""OBS WebSocket integration: auth, read-only allowlist, screenshots, disconnect/reconnect (13)."""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from gameoverlay.demo.fake_obs import FakeOBSServer
from gameoverlay.obs.client import OBSClient, OBSNotConnected, OBSRequestError, compute_auth


def _jpeg(color=(200, 50, 50), size=(64, 36)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
async def obs_pair():
    server = FakeOBSServer(lambda name, w, q: _jpeg(), password="hunter2-obs")
    port = await server.start()
    pw = {"v": "hunter2-obs"}
    client = OBSClient(lambda: ("127.0.0.1", port), lambda: pw["v"], max_backoff_s=0.5)
    client.start()
    yield server, client, pw
    await client.stop()
    await server.stop()


def test_auth_string_matches_protocol_spec():
    # Values computed per the obs-websocket v5 authentication spec.
    import base64
    import hashlib

    secret = base64.b64encode(hashlib.sha256(b"pw" + b"salt").digest()).decode()
    expected = base64.b64encode(hashlib.sha256((secret + "chal").encode()).digest()).decode()
    assert compute_auth("pw", "salt", "chal") == expected


async def test_connect_list_and_screenshot(obs_pair):
    server, client, _ = obs_pair
    assert await client.wait_connected(3)
    inputs = await client.list_inputs()
    assert inputs[0]["name"] == "Sony ZV-1" and inputs[0]["isCamera"]
    overlay = next(i for i in inputs if i["kind"] == "browser_source")
    assert overlay["selectable"] is False, "the overlay browser source must not be selectable as camera"
    img = await client.screenshot("Sony ZV-1", width=64)
    assert Image.open(io.BytesIO(img)).size == (64, 36)
    assert client.status()["obsVersion"] == "31.0.0"


async def test_unknown_source_errors(obs_pair):
    _, client, _ = obs_pair
    assert await client.wait_connected(3)
    with pytest.raises(OBSRequestError):
        await client.screenshot("Nope")


async def test_never_sends_state_changing_requests(obs_pair):
    server, client, _ = obs_pair
    assert await client.wait_connected(3)
    for forbidden in ("StartStream", "StopStream", "StartRecord", "SetCurrentProgramScene", "SetInputSettings"):
        with pytest.raises(PermissionError):
            await client.request(forbidden)
    await client.screenshot("Sony ZV-1")
    assert set(server.requests_seen) <= {"GetVersion", "GetInputList", "GetSourceScreenshot", "GetSourceActive"}


async def test_wrong_password_reports_auth_failed():
    server = FakeOBSServer(lambda n, w, q: _jpeg(), password="right")
    port = await server.start()
    client = OBSClient(lambda: ("127.0.0.1", port), lambda: "wrong")
    client.start()
    for _ in range(50):
        if client.state == "auth_failed":
            break
        await asyncio.sleep(0.05)
    assert client.state == "auth_failed"
    assert "right" not in (client.last_error or "")
    await client.stop()
    await server.stop()


async def test_reconnects_after_obs_restart():
    server = FakeOBSServer(lambda n, w, q: _jpeg(), password=None)
    port = await server.start()
    client = OBSClient(lambda: ("127.0.0.1", port), lambda: None, max_backoff_s=0.4)
    client.start()
    assert await client.wait_connected(3)

    # OBS crashes / closes.
    await server.drop_all()
    await server.stop()
    for _ in range(40):
        if not client.connected:
            break
        await asyncio.sleep(0.05)
    assert not client.connected
    with pytest.raises(OBSNotConnected):
        await client.screenshot("Sony ZV-1")

    # OBS comes back on the same port.
    server2 = FakeOBSServer(lambda n, w, q: _jpeg((0, 255, 0)), password=None)
    await server2.start(port=port)
    assert await client.wait_connected(5)
    img = await client.screenshot("Sony ZV-1")
    assert Image.open(io.BytesIO(img)).getpixel((5, 5))[1] > 200
    assert client.reconnects >= 1
    await client.stop()
    await server2.stop()


async def test_pending_request_fails_cleanly_on_disconnect():
    server = FakeOBSServer(lambda n, w, q: _jpeg(), password=None)
    server.screenshot_delay_s = 2.0
    port = await server.start()
    client = OBSClient(lambda: ("127.0.0.1", port), lambda: None, max_backoff_s=0.3)
    client.start()
    assert await client.wait_connected(3)
    task = asyncio.create_task(client.screenshot("Sony ZV-1"))
    await asyncio.sleep(0.1)
    await server.drop_all()
    with pytest.raises((OBSNotConnected,)):
        await asyncio.wait_for(task, 3)
    await client.stop()
    await server.stop()
