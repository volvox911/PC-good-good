"""Local-only access control for the private operator dashboard.

* **Host check** (all HTTP + WebSocket): requests must address the app as
  ``127.0.0.1``/``localhost``/``[::1]`` on its port — blocks DNS-rebinding attacks
  from web pages in the streaming PC's browser.
* **Authentication**: a 256-bit admin token (created on first run, stored in the
  per-user data directory). ``/login?token=...`` exchanges it for an HttpOnly,
  SameSite=Strict session cookie signed with HMAC — stateless, so it survives app
  restarts. Scripts may instead send ``Authorization: Bearer <token>``.
* **CSRF**: state-changing requests with a cookie session must carry
  ``X-CSRF-Token`` (derived from the session) and, if present, a same-origin
  ``Origin`` header.
* **Public routes** are limited to the read-only overlay, its static assets and a
  minimal health check. They never include secrets or admin data.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Iterable

from fastapi import HTTPException, Request, WebSocket

SESSION_COOKIE = "wgo_session"
CSRF_COOKIE = "wgo_csrf"
SESSION_MAX_AGE_S = 30 * 24 * 3600
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def allowed_hosts(host: str, port: int) -> set[str]:
    names = {"127.0.0.1", "localhost", "[::1]"}
    if host not in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
        names.add(host)
    hosts = {f"{n}:{port}" for n in names}
    if port == 80:  # browsers omit the default port from the Host header
        hosts |= names
    return hosts


def allowed_origins(hosts: Iterable[str]) -> set[str]:
    return {f"http://{h}" for h in hosts}


class Auth:
    def __init__(self, admin_token: str) -> None:
        self._token = admin_token
        self._key = hashlib.sha256(("wgo-session:" + admin_token).encode()).digest()
        self._codes: dict[str, float] = {}

    # One-time sign-in codes: the launcher opens /login?code=..., so the permanent admin
    # token never appears in a URL or the browser history. Codes are single-use and expire.
    def issue_code(self, ttl_s: float = 900.0, code: str | None = None) -> str:
        code = code or secrets.token_urlsafe(24)
        now = time.time()
        self._codes = {c: exp for c, exp in self._codes.items() if exp > now}
        self._codes[code] = now + ttl_s
        return code

    def redeem_code(self, code: str | None) -> bool:
        if not code:
            return False
        for c in list(self._codes):
            if hmac.compare_digest(c.encode(), code.encode()):
                exp = self._codes.pop(c)  # single use
                return exp > time.time()
        return False

    def token_ok(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self._token.encode())

    def _sig(self, msg: str) -> str:
        return hmac.new(self._key, msg.encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> tuple[str, str]:
        nonce = f"{int(time.time())}-{secrets.token_urlsafe(16)}"
        return f"{nonce}.{self._sig(nonce)}", self.csrf_for(nonce)

    def csrf_for(self, nonce: str) -> str:
        return self._sig("csrf:" + nonce)[:48]

    def session_nonce(self, cookie: str | None) -> str | None:
        if not cookie or "." not in cookie:
            return None
        nonce, sig = cookie.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(nonce)):
            return None
        try:
            issued = int(nonce.split("-", 1)[0])
        except ValueError:
            return None
        if time.time() - issued > SESSION_MAX_AGE_S:
            return None
        return nonce

    def bearer_ok(self, request_headers) -> bool:
        authz = request_headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            return self.token_ok(authz[7:].strip())
        return False


def require_admin(request: Request) -> str:
    """FastAPI dependency for every private endpoint."""
    auth: Auth = request.app.state.auth
    if auth.bearer_ok(request.headers):
        return "bearer"
    nonce = auth.session_nonce(request.cookies.get(SESSION_COOKIE))
    if nonce is None:
        raise HTTPException(status_code=401, detail="Not signed in to the operator dashboard")
    if request.method in UNSAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, auth.csrf_for(nonce)):
            raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")
        origin = request.headers.get("origin")
        if origin and origin not in request.app.state.allowed_origins:
            raise HTTPException(status_code=403, detail="Cross-origin request refused")
    return "cookie"


def websocket_admin_ok(ws: WebSocket) -> bool:
    auth: Auth = ws.app.state.auth
    origin = ws.headers.get("origin")
    if origin and origin not in ws.app.state.allowed_origins:
        return False
    if auth.bearer_ok(ws.headers):
        return True
    return auth.session_nonce(ws.cookies.get(SESSION_COOKIE)) is not None


class HostCheckMiddleware:
    """Pure ASGI middleware rejecting requests whose Host header is not ours."""

    def __init__(self, app, hosts: set[str]) -> None:
        self.app = app
        self.hosts = {h.lower() for h in hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            host = ""
            for k, v in scope.get("headers", []):
                if k == b"host":
                    host = v.decode("latin-1").lower()
                    break
            if host not in self.hosts:
                if scope["type"] == "http":
                    body = b"Invalid Host header"
                    await send({"type": "http.response.start", "status": 400,
                                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body})
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                ]
                if path.startswith("/overlay"):
                    csp = (
                        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                        "img-src 'self' data:; connect-src 'self' ws: wss:; font-src 'self' data:; "
                        "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
                    )
                else:
                    csp = (
                        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                        "img-src 'self' data: blob:; connect-src 'self' ws: wss:; frame-src 'self'; "
                        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                    )
                    headers.append((b"x-frame-options", b"DENY"))
                headers.append((b"content-security-policy", csp.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)
