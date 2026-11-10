"""FastAPI application: public read-only overlay + private operator dashboard/API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError

from .config import AppConfig
from .demo.sample import PREVIEW_VARIANTS
from .obs.client import OBSError
from .pricing.client import PriceChartingAuthError, PriceChartingError
from .pricing.service import PriceUnavailable
from .secrets_store import ALL_SECRETS
from .security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    Auth,
    HostCheckMiddleware,
    SecurityHeadersMiddleware,
    allowed_hosts,
    allowed_origins,
    require_admin,
    websocket_admin_ok,
)
from .services import Services
from .vision.imaging import decode, encode_jpeg, limit_long_side, rotate

log = logging.getLogger(__name__)

WEB_DIR = Path(str(resources.files("gameoverlay").joinpath("web")))


def _web(*parts: str) -> Path:
    p = WEB_DIR.joinpath(*parts).resolve()
    if WEB_DIR.resolve() not in p.parents:
        raise HTTPException(404)
    if not p.is_file():
        raise HTTPException(404)
    return p


def _static(sub: str, name: str) -> FileResponse:
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(404)
    p = _web(sub, name)
    media = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml"}.get(p.suffix)
    return FileResponse(p, media_type=media)


class SelectBody(BaseModel):
    productId: str = Field(pattern=r"^\d{1,12}$")


class UPCBody(BaseModel):
    upc: str = Field(min_length=8, max_length=20)


class HoldBody(BaseModel):
    on: bool


class SecretsBody(BaseModel):
    anthropic_api_key: str | None = Field(default=None, max_length=400)
    pricecharting_token: str | None = Field(default=None, max_length=200)
    obs_password: str | None = Field(default=None, max_length=200)


def create_app(config: AppConfig, services: Services | None = None) -> FastAPI:
    svc = services or Services(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await svc.start()
        log.info("dashboard: %s/login?token=<admin token>   overlay: %s/overlay", config.base_url, config.base_url)
        try:
            yield
        finally:
            await svc.stop()
            log.info("shutdown complete")

    app = FastAPI(title="Whatnot AI Game Overlay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    hosts = allowed_hosts(config.host, config.port)
    app.state.services = svc
    app.state.auth = Auth(svc.admin_token)
    app.state.allowed_origins = allowed_origins(hosts)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(HostCheckMiddleware, hosts=(hosts | {"testserver"}) if config.env.value == "test" else hosts)

    admin = Depends(require_admin)

    # ================================================================ public: overlay
    @app.get("/overlay", response_class=HTMLResponse)
    async def overlay_page() -> Response:
        return FileResponse(_web("overlay", "index.html"), media_type="text/html")

    @app.get("/overlay/static/{name}")
    async def overlay_static(name: str) -> Response:
        return _static("overlay", name)

    @app.websocket("/ws/overlay")
    async def overlay_ws(ws: WebSocket) -> None:
        await svc.hub.handle_client(ws)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "uptimeS": int(time.time() - svc.started_at)}

    # ================================================================ auth
    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request, token: str | None = None) -> Response:
        auth: Auth = app.state.auth
        if token is not None:
            if not auth.token_ok(token):
                await asyncio.sleep(0.5)
                return HTMLResponse(_login_html("That access link is not valid."), status_code=401)
            cookie, csrf = auth.new_session()
            resp = RedirectResponse("/dashboard", status_code=303)
            resp.set_cookie(SESSION_COOKIE, cookie, max_age=SESSION_MAX_AGE_S, httponly=True, samesite="strict", path="/")
            resp.set_cookie(CSRF_COOKIE, csrf, max_age=SESSION_MAX_AGE_S, httponly=False, samesite="strict", path="/")
            return resp
        return HTMLResponse(_login_html(None))

    @app.post("/logout")
    async def logout(_: str = admin) -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        resp.delete_cookie(CSRF_COOKIE, path="/")
        return resp

    # ================================================================ private: dashboard
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        if app.state.auth.session_nonce(request.cookies.get(SESSION_COOKIE)) is None:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(_web("dashboard", "index.html"), media_type="text/html")

    @app.get("/dashboard/static/{name}")
    async def dashboard_static(name: str) -> Response:
        return _static("dashboard", name)

    @app.websocket("/ws/dashboard")
    async def dashboard_ws(ws: WebSocket) -> None:
        if not websocket_admin_ok(ws):
            await ws.close(code=1008)
            return
        await ws.accept()
        changed = asyncio.Event()
        changed.set()
        svc.on_change(changed.set)
        try:
            while True:
                try:
                    await asyncio.wait_for(changed.wait(), 2.0)
                except asyncio.TimeoutError:
                    pass
                changed.clear()
                await asyncio.wait_for(ws.send_text(json.dumps({"type": "status", "status": svc.status()}, default=str)), 3)
                await asyncio.sleep(0.2)  # coalesce bursts (max ~5 updates/s)
        except Exception:
            pass
        finally:
            try:
                svc._listeners.remove(changed.set)
            except ValueError:
                pass

    @app.get("/api/status")
    async def api_status(_: str = admin) -> dict:
        return svc.status()

    @app.get("/api/frame/current.jpg")
    async def api_frame(_: str = admin) -> Response:
        data = svc.engine.current.frame_jpeg
        if not data:
            raise HTTPException(404, "no captured frame yet")
        return Response(data, media_type="image/jpeg")

    @app.get("/api/camera/preview.jpg")
    async def api_camera_preview(_: str = admin) -> Response:
        s = svc.settings
        if not s.obs.camera_source:
            raise HTTPException(409, "select a camera source first")
        try:
            data = await svc.obs.screenshot(s.obs.camera_source, width=960, quality=80)
        except OBSError as e:
            raise HTTPException(503, f"OBS: {e}")
        img = limit_long_side(rotate(decode(data), s.obs.rotation), 960)
        return Response(encode_jpeg(img, 80), media_type="image/jpeg")

    # -- detection / overlay controls -------------------------------------------------------
    @app.post("/api/detection/pause")
    async def api_pause(_: str = admin) -> dict:
        svc.engine.pause()
        return {"ok": True}

    @app.post("/api/detection/resume")
    async def api_resume(_: str = admin) -> dict:
        svc.engine.resume()
        return {"ok": True}

    @app.post("/api/detection/rescan")
    async def api_rescan(_: str = admin) -> dict:
        svc.engine.rescan()
        return {"ok": True}

    @app.post("/api/overlay/clear")
    async def api_clear(_: str = admin) -> dict:
        svc.engine.clear_overlay()
        return {"ok": True}

    @app.post("/api/overlay/hold")
    async def api_hold(body: HoldBody, _: str = admin) -> dict:
        svc.engine.hold(body.on)
        return {"ok": True, "held": svc.engine.pinned}

    @app.post("/api/overlay/show-pending")
    async def api_show_pending(_: str = admin) -> dict:
        return {"ok": svc.engine.show_pending()}

    @app.post("/api/prices/refresh")
    async def api_refresh(_: str = admin) -> dict:
        try:
            rec = await svc.engine.refresh_prices()
        except (PriceUnavailable, PriceChartingError) as e:
            raise HTTPException(502, str(e))
        return {"ok": rec is not None, "record": rec.to_overlay_dict() if rec else None}

    # -- manual identification ------------------------------------------------------------------
    @app.get("/api/search")
    async def api_search(q: str = Query(min_length=1, max_length=200), remote: bool = True, _: str = admin) -> dict:
        local = [c.to_dict() for c in svc.pricing.local_suggestions(q)]
        out: dict = {"local": local, "remote": [], "error": None}
        if remote:
            try:
                out["remote"] = [c.to_dict() for c in await svc.pricing.search(q)]
            except PriceChartingAuthError as e:
                out["error"] = str(e)
            except PriceChartingError as e:
                out["error"] = str(e)
        return out

    @app.post("/api/select")
    async def api_select(body: SelectBody, _: str = admin) -> dict:
        try:
            rec = await svc.engine.select_product(body.productId, source="manual")
        except (PriceUnavailable, PriceChartingError) as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "record": rec.to_overlay_dict() if rec else None}

    @app.post("/api/upc")
    async def api_upc(body: UPCBody, _: str = admin) -> dict:
        try:
            rec = await svc.engine.lookup_upc(body.upc)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except (PriceUnavailable, PriceChartingError) as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "record": rec.to_overlay_dict() if rec else None}

    @app.get("/api/history")
    async def api_history(limit: int = 100, _: str = admin) -> dict:
        return {"items": svc.engine.history(limit)}

    @app.post("/api/history/{history_id}/recall")
    async def api_recall(history_id: int, _: str = admin) -> dict:
        try:
            rec = await svc.engine.recall(history_id)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except (PriceUnavailable, PriceChartingError) as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "record": rec.to_overlay_dict() if rec else None}

    # -- background reference -----------------------------------------------------------------
    @app.post("/api/background/capture")
    async def api_bg_capture(_: str = admin) -> dict:
        try:
            await svc.engine.capture_background()
        except (OBSError, ValueError) as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.delete("/api/background")
    async def api_bg_clear(_: str = admin) -> dict:
        svc.engine.clear_background()
        return {"ok": True}

    # -- settings & secrets ---------------------------------------------------------------------
    @app.get("/api/settings")
    async def api_settings(_: str = admin) -> dict:
        return svc.settings.model_dump()

    @app.patch("/api/settings")
    async def api_settings_patch(request: Request, _: str = admin) -> dict:
        try:
            patch = await request.json()
            if not isinstance(patch, dict):
                raise ValueError("settings patch must be a JSON object")
            new = svc.update_settings(patch)
        except ValidationError as e:
            raise HTTPException(422, json.loads(e.json(include_url=False, include_input=False)))
        except ValueError as e:
            raise HTTPException(422, str(e))
        return new.model_dump()

    @app.get("/api/secrets")
    async def api_secrets(_: str = admin) -> dict:
        return {"backend": svc.secrets.backend, **svc.secrets.all_status()}

    @app.put("/api/secrets")
    async def api_secrets_put(body: SecretsBody, _: str = admin) -> dict:
        changed = []
        for name in ALL_SECRETS:
            v = getattr(body, name)
            if v is not None:
                try:
                    svc.secrets.set(name, v)
                except ValueError as e:
                    raise HTTPException(422, str(e))
                changed.append(name)
        if "obs_password" in changed and not svc.demo:
            svc.obs.reconnect_now()
        if "pricecharting_token" in changed:
            svc.pc_client.auth_failed = False
        return {"ok": True, "changed": changed, **svc.secrets.all_status()}

    @app.get("/api/obs/sources")
    async def api_obs_sources(_: str = admin) -> dict:
        if not svc.obs.connected:
            raise HTTPException(503, f"OBS not connected ({svc.obs.state})")
        try:
            return {"inputs": await svc.obs.list_inputs()}
        except OBSError as e:
            raise HTTPException(503, str(e))

    @app.post("/api/obs/reconnect")
    async def api_obs_reconnect(_: str = admin) -> dict:
        svc.obs.reconnect_now()
        ok = await svc.obs.wait_connected(5)
        return {"ok": ok, **svc.obs.status()}

    @app.get("/api/preview/{variant}")
    async def api_preview(variant: str, _: str = admin) -> dict:
        rec = PREVIEW_VARIANTS.get(variant)
        if rec is None:
            raise HTTPException(404)
        return {"record": {**rec, "retrievedAt": _iso_now()}, "layout": svc.hub.layout(), "demo": True}

    return app


def app_factory() -> FastAPI:
    """Factory used by ``uvicorn --factory`` / development auto-reload (child process)."""
    from .logging_setup import setup_logging
    from .secrets_store import SecretStore

    cfg = AppConfig()
    store = SecretStore(cfg.data_dir, use_keyring=not cfg.disable_keyring)
    setup_logging("DEBUG" if cfg.env.value == "development" else cfg.log_level, cfg.log_dir,
                  secret_source=store.known_values)
    return create_app(cfg)


def _iso_now() -> str:
    from .models import iso

    return iso(time.time())


def _login_html(error: str | None) -> str:
    err = f'<p class="err">{error}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Operator Sign-in</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/dashboard/static/dashboard.css"></head>
<body class="login"><main class="login-card"><h1>Price-O-Matic Operator</h1>
<p>Open the dashboard with the one-click link printed by the launcher, or paste your access token.</p>{err}
<form method="get" action="/login"><label>Access token<input name="token" type="password" autocomplete="off" required></label>
<button type="submit">Sign in</button></form>
<p class="hint">The token is stored in the app's data folder (<code>admin_token</code>).</p></main></body></html>"""
