"""Protection against unauthorized dashboard access (20), demo-data prevention, public surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gameoverlay.config import AppConfig, Environment
from gameoverlay.demo.mock_pricecharting import DEMO_TOKEN, MockPriceCharting
from gameoverlay.models import VerifiedRecord
from gameoverlay.overlay.hub import DemoDataRejected
from gameoverlay.server import create_app
from gameoverlay.services import Services

SAMPLE_STRINGS = ["42.50", "109.99", "599.00", "4250", "10999", "59900", "Ocarina", "SUPER MARIO", "Majora"]


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICECHARTING_TOKEN", DEMO_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret-value-123456")
    monkeypatch.setenv("OBS_PASSWORD", "obs-secret-password-xyz")
    cfg = AppConfig(env=Environment.TEST, data_dir=tmp_path, port=8765)
    mock = MockPriceCharting()
    svc = Services(cfg, pc_transport=mock.transport())
    app = create_app(cfg, svc)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.svc = svc
        client.mock = mock
        yield client


def login(client):
    r = client.post("/login", data={"token": client.svc.admin_token}, follow_redirects=False)
    assert r.status_code == 303
    return client.cookies.get("wgo_csrf")


def test_public_overlay_has_no_secrets_or_sample_data(live):
    for path in ["/overlay", "/overlay/static/overlay.js", "/overlay/static/overlay-core.js", "/overlay/static/style.css"]:
        r = live.get(path)
        assert r.status_code == 200, path
        body = r.text
        for secret in ("sk-ant-test-secret-value-123456", "obs-secret-password-xyz", DEMO_TOKEN, live.svc.admin_token):
            assert secret not in body
        if path != "/overlay/static/style.css":
            for s in SAMPLE_STRINGS:
                assert s not in body, f"sample value {s!r} found in live overlay asset {path}"
        assert "/api/" not in body, "overlay assets must not reference admin routes"


def test_bundled_fonts_are_served_locally(live):
    """The exact families from the design's font guide, served by the app (no CDN)."""
    import re

    for name in ("knewave.ttf", "anton.ttf", "barlow-condensed-800.ttf", "barlow-condensed-900-italic.ttf"):
        r = live.get(f"/overlay/static/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "font/ttf" and len(r.content) > 10_000
    for name in ("OFL-Knewave.txt", "OFL-Anton.txt", "OFL-BarlowCondensed.txt"):
        assert "SIL OPEN FONT LICENSE" in live.get(f"/overlay/static/{name}").text.upper()
    css = live.get("/overlay/static/style.css").text
    for family in ("Knewave", "Anton", "Barlow Condensed"):
        assert re.search(rf"@font-face\s*\{{\s*font-family:'{family}'", css), family
    for target in re.findall(r"url\(\s*[\"']?([^\"')]+)", css):
        assert target.startswith(("data:", "/overlay/static/", "%23")), f"external dependency: {target[:60]}"
    assert "@import" not in css and "googleapis" not in live.get("/overlay").text


def test_frame_skin_is_served_unchanged(live):
    import hashlib

    r = live.get("/overlay/static/header-skin.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    # the operator's text-free frame from the approved design, byte for byte
    assert hashlib.sha256(r.content).hexdigest() == "56ce28994a88944e5426f1b38356403c011420fba56e6a38f0f45ade8b9b5d5d"


def test_health_is_minimal(live):
    r = live.get("/health")
    assert r.status_code == 200 and set(r.json()) == {"status", "uptimeS"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/status"), ("GET", "/api/settings"), ("GET", "/api/secrets"), ("GET", "/api/history"),
    ("GET", "/api/search?q=mario"), ("GET", "/api/frame/current.jpg"), ("GET", "/api/preview/sample"),
    ("POST", "/api/overlay/clear"), ("POST", "/api/select"), ("PATCH", "/api/settings"), ("PUT", "/api/secrets"),
    ("POST", "/api/detection/pause"),
])
def test_admin_routes_require_auth(live, method, path):
    r = live.request(method, path, json={})
    assert r.status_code == 401


def test_dashboard_redirects_to_login(live):
    r = live.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bad_token_rejected(live):
    r = live.post("/login", data={"token": "nope"}, follow_redirects=False)
    assert r.status_code == 401
    assert "wgo_session" not in live.cookies


def test_token_is_never_accepted_in_url(live):
    r = live.get(f"/login?token={live.svc.admin_token}", follow_redirects=False)
    assert r.status_code == 200 and "wgo_session" not in live.cookies  # just the sign-in form


def test_one_time_code_is_single_use(live):
    code = live.app.state.auth.issue_code()
    r = live.get(f"/login?code={code}", follow_redirects=False)
    assert r.status_code == 303 and "wgo_session" in live.cookies
    live.cookies.clear()
    r2 = live.get(f"/login?code={code}", follow_redirects=False)
    assert r2.status_code == 401 and "wgo_session" not in live.cookies


def test_expired_code_rejected(live):
    code = live.app.state.auth.issue_code(ttl_s=-1)
    assert live.get(f"/login?code={code}", follow_redirects=False).status_code == 401


def test_login_sets_httponly_strict_cookie(live):
    r = live.post("/login", data={"token": live.svc.admin_token}, follow_redirects=False)
    cookies = r.headers.get_list("set-cookie")
    sess = next(c for c in cookies if c.startswith("wgo_session="))
    assert "HttpOnly" in sess and "SameSite=strict" in sess
    assert live.get("/api/status").status_code == 200


def test_csrf_required_for_state_changes(live):
    csrf = login(live)
    assert live.post("/api/overlay/clear").status_code == 403
    assert live.post("/api/overlay/clear", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert live.post("/api/overlay/clear", headers={"X-CSRF-Token": csrf}).status_code == 200


def test_cross_origin_requests_refused(live):
    csrf = login(live)
    r = live.post("/api/overlay/clear", headers={"X-CSRF-Token": csrf, "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_forged_session_cookie_rejected(live):
    live.cookies.set("wgo_session", "123-abc.deadbeef")
    assert live.get("/api/status").status_code == 401


def test_bearer_token_for_scripts(live):
    r = live.get("/api/status", headers={"Authorization": f"Bearer {live.svc.admin_token}"})
    assert r.status_code == 200
    assert live.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_dns_rebinding_host_header_rejected(live):
    r = live.get("/overlay", headers={"host": "attacker.example:8765"})
    assert r.status_code == 400
    r = live.get("/api/status", headers={"host": "attacker.example", "Authorization": f"Bearer {live.svc.admin_token}"})
    assert r.status_code == 400


def test_secrets_are_write_only(live):
    csrf = login(live)
    st = live.get("/api/secrets").json()
    assert st["anthropic_api_key"] == {"configured": True, "source": "env"}
    text = live.get("/api/status").text + live.get("/api/settings").text
    for secret in ("sk-ant-test-secret-value-123456", "obs-secret-password-xyz", DEMO_TOKEN):
        assert secret not in text
    r = live.put("/api/secrets", json={"obs_password": "new-pass-123"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and "new-pass-123" not in r.text


def test_settings_validation(live):
    csrf = login(live)
    h = {"X-CSRF-Token": csrf}
    assert live.patch("/api/settings", json={"pricing": {"min_request_interval_s": 0.2}}, headers=h).status_code == 422
    assert live.patch("/api/settings", json={"overlay": {"position": "middle"}}, headers=h).status_code == 422
    r = live.patch("/api/settings", json={"overlay": {"size": "large", "position": "bottom-left"}}, headers=h)
    assert r.status_code == 200 and r.json()["overlay"]["size"] == "large"
    assert live.svc.settings.overlay.position == "bottom-left"


def test_static_path_traversal(live):
    assert live.get("/overlay/static/..%2F..%2Fserver.py").status_code == 404
    assert live.get("/overlay/static/.hidden").status_code == 404
    assert live.get("/dashboard/static/..%2Findex.html").status_code == 404


def test_security_headers(live):
    login(live)
    d = live.get("/dashboard")
    assert d.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in d.headers["content-security-policy"]
    o = live.get("/overlay")
    assert "script-src 'self'" in o.headers["content-security-policy"]
    assert o.headers["x-content-type-options"] == "nosniff"


def test_dashboard_ws_requires_auth(live):
    with pytest.raises(WebSocketDisconnect):
        with live.websocket_connect("/ws/dashboard") as ws:
            ws.receive_text()


def test_dashboard_ws_rejects_foreign_origin(live):
    login(live)
    with pytest.raises(WebSocketDisconnect):
        with live.websocket_connect("/ws/dashboard", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_text()


def test_overlay_ws_rejects_foreign_origin(live):
    with pytest.raises(WebSocketDisconnect):
        with live.websocket_connect("/ws/overlay", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_text()
    with live.websocket_connect("/ws/overlay", headers={"origin": "http://127.0.0.1:8765"}) as ws:
        assert ws.receive_json()["type"] == "state"


def test_overlay_ws_is_read_only_and_starts_empty(live):
    with live.websocket_connect("/ws/overlay") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "state" and msg["visible"] is False and msg["record"] is None
        assert msg["demoMode"] is False
        ws.send_text('{"type":"show","record":{"title":"hack"}}')  # ignored
        ws.send_text("garbage")
        assert live.svc.hub.current is None
        for secret in ("sk-ant", "obs-secret", DEMO_TOKEN):
            assert secret not in str(msg)


def test_demo_records_rejected_in_live_mode(live):
    rec = VerifiedRecord(title="X", platform="NES", product_id="1", loose_cents=1, cib_cents=None, new_cents=None,
                         retrieved_at=0, freshness="fresh", source="auto", demo=True)
    with pytest.raises(DemoDataRejected):
        live.svc.hub.show(rec)
    rec2 = VerifiedRecord(title="X", platform="NES", product_id="1", loose_cents=1, cib_cents=None, new_cents=None,
                          retrieved_at=0, freshness="fresh", source="demo", demo=False)
    with pytest.raises(DemoDataRejected):
        live.svc.hub.show(rec2)
    with pytest.raises(TypeError):
        live.svc.hub.show({"title": "dict records are not allowed"})


def test_preview_samples_never_reach_overlay(live):
    login(live)
    r = live.get("/api/preview/sample")
    assert r.status_code == 200 and r.json()["record"]["demo"] is True
    assert live.svc.hub.current is None


def test_manual_select_drives_overlay(live):
    csrf = login(live)
    with live.websocket_connect("/ws/overlay") as ws:
        assert ws.receive_json()["visible"] is False
        r = live.post("/api/select", json={"productId": "900101"}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200
        msg = ws.receive_json()
        while not msg.get("visible"):
            msg = ws.receive_json()
        rec = msg["record"]
        assert rec["productId"] == "900101" and rec["verified"] is True and rec["demo"] is False
        assert (rec["looseCents"], rec["completeCents"], rec["newCents"]) == (4250, 10999, 59900)
        assert rec["looseUsd"] == 42.5
        assert set(rec) == {"title", "platform", "productId", "verified", "looseUsd", "completeUsd", "newUsd",
                            "looseCents", "completeCents", "newCents", "retrievedAt", "freshness", "demo"}
        live.post("/api/overlay/clear", headers={"X-CSRF-Token": csrf})
        msg = ws.receive_json()
        assert msg["visible"] is False and msg["record"] is None


def test_select_rejects_invalid_ids(live):
    csrf = login(live)
    h = {"X-CSRF-Token": csrf}
    assert live.post("/api/select", json={"productId": "../etc"}, headers=h).status_code == 422
    assert live.post("/api/select", json={"productId": "999999999"}, headers=h).status_code == 502
    assert live.svc.hub.current is None


def test_missing_credentials_startup(tmp_path, monkeypatch):
    for v in ("PRICECHARTING_TOKEN", "ANTHROPIC_API_KEY", "OBS_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    cfg = AppConfig(env=Environment.TEST, data_dir=tmp_path, port=8765)
    app = create_app(cfg, Services(cfg))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        st = client.get("/api/status", headers={"Authorization": f"Bearer {client.app.state.services.admin_token}"}).json()
        assert st["anthropic"]["state"] == "not_configured"
        assert st["pricecharting"]["state"] == "not_configured"
        assert st["secrets"]["pricecharting_token"]["configured"] is False
        assert client.get("/overlay").status_code == 200  # app still serves safely


def test_overlay_page_is_stamped_with_build(live):
    import re

    html = live.get("/overlay").text
    m = re.search(r'name="overlay-build" content="([0-9a-f]{12})"', html)
    assert m, "overlay page carries its build id"
    for asset in ("style.css", "overlay.js", "header-skin.png"):
        assert f"/overlay/static/{asset}?v={m.group(1)}" in html, asset
    with live.websocket_connect("/ws/overlay") as ws:
        assert ws.receive_json()["build"] == m.group(1)
