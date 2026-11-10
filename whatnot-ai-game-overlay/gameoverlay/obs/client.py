"""Read-only client for OBS Studio's built-in obs-websocket v5 server.

Protocol (obs-websocket 5.x, ``rpcVersion`` 1, subprotocol ``obswebsocket.json``):
Hello (op 0) -> Identify (op 1) -> Identified (op 2); Request (op 6) /
RequestResponse (op 7). Authentication:
``secret = b64(sha256(password + salt))``, ``auth = b64(sha256(secret + challenge))``.

Safety: only the request types in :data:`READ_ONLY_REQUESTS` can be sent. This
client can never start/stop streaming or recording, switch scenes, or change any
OBS setting. Screenshots are taken of the selected camera *input* via
``GetSourceScreenshot`` — not of the program output — so the overlay itself is
never captured (no recursive recognition).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import random
import time
import uuid
from typing import Callable

import websockets
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

READ_ONLY_REQUESTS = frozenset(
    {
        "GetVersion",
        "GetStats",
        "GetVideoSettings",
        "GetInputList",
        "GetInputKindList",
        "GetSceneList",
        "GetSourceActive",
        "GetSourceScreenshot",
    }
)

# Input kinds that are cameras/capture devices (preferred in the source dropdown).
CAMERA_KINDS = (
    "dshow_input",  # Windows "Video Capture Device" (Sony ZV-1 via USB / capture card)
    "av_capture_input",  # macOS
    "macos-avcapture",
    "v4l2_input",  # Linux
    "decklink-input",
    "ndi_source",
    "elgato",
    "pipewire-camera",
)
# Kinds that must never be used as the recognition source (would capture the overlay).
FORBIDDEN_SOURCE_KINDS = ("browser_source", "text_gdiplus", "text_ft2_source", "color_source")

AUTH_FAILED_CLOSE_CODE = 4009


class OBSError(Exception):
    pass


class OBSNotConnected(OBSError):
    pass


class OBSAuthError(OBSError):
    pass


class OBSRequestError(OBSError):
    def __init__(self, request_type: str, code: int, comment: str | None) -> None:
        super().__init__(f"OBS request {request_type} failed (code {code}): {comment or 'no detail'}")
        self.code = code


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class OBSClient:
    def __init__(
        self,
        address: Callable[[], tuple[str, int]],
        password: Callable[[], str | None],
        request_timeout_s: Callable[[], float] = lambda: 6.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._address = address
        self._password = password
        self._timeout = request_timeout_s
        self._max_backoff = max_backoff_s
        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._connected_evt = asyncio.Event()
        self.state = "disconnected"
        self.last_error: str | None = None
        self.connected_since: float | None = None
        self.reconnects = 0
        self.version: dict = {}
        self.image_formats: list[str] = []
        self._listeners: list[Callable[[str], None]] = []

    # -- lifecycle ----------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="obs-connection")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        ws = self._ws
        if ws is not None:
            await ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._set_state("disconnected")

    def reconnect_now(self) -> None:
        """Settings changed (host/port/password): drop and reconnect immediately."""
        self._wake.set()
        ws = self._ws
        if ws is not None:
            asyncio.create_task(ws.close())

    def on_state(self, cb: Callable[[str], None]) -> None:
        self._listeners.append(cb)

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def status(self) -> dict:
        host, port = self._address()
        return {
            "state": self.state,
            "address": f"{host}:{port}",
            "lastError": self.last_error,
            "connectedSince": self.connected_since,
            "reconnects": self.reconnects,
            "obsVersion": self.version.get("obsVersion"),
            "websocketVersion": self.version.get("obsWebSocketVersion"),
        }

    # -- requests -----------------------------------------------------------------------
    async def request(self, request_type: str, data: dict | None = None, timeout: float | None = None) -> dict:
        if request_type not in READ_ONLY_REQUESTS:
            raise PermissionError(f"OBS request {request_type!r} is not allowed (read-only client)")
        ws = self._ws
        if ws is None or not self.connected:
            raise OBSNotConnected("not connected to OBS")
        rid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await ws.send(
                json.dumps(
                    {"op": 6, "d": {"requestType": request_type, "requestId": rid, "requestData": data or {}}}
                )
            )
            msg = await asyncio.wait_for(fut, timeout or self._timeout())
        except ConnectionClosed:
            raise OBSNotConnected("OBS connection closed") from None
        except asyncio.TimeoutError:
            raise OBSError(f"OBS request {request_type} timed out") from None
        finally:
            self._pending.pop(rid, None)
        status = msg.get("requestStatus") or {}
        if not status.get("result"):
            raise OBSRequestError(request_type, int(status.get("code") or 0), status.get("comment"))
        return msg.get("responseData") or {}

    async def list_inputs(self) -> list[dict]:
        data = await self.request("GetInputList")
        out = []
        for inp in data.get("inputs") or []:
            name = inp.get("inputName")
            kind = inp.get("inputKind") or inp.get("unversionedInputKind") or ""
            if not isinstance(name, str):
                continue
            out.append(
                {
                    "name": name,
                    "kind": kind,
                    "isCamera": any(kind.startswith(k) for k in CAMERA_KINDS),
                    "selectable": not any(kind.startswith(k) for k in FORBIDDEN_SOURCE_KINDS),
                }
            )
        out.sort(key=lambda i: (not i["isCamera"], i["name"].lower()))
        return out

    async def source_active(self, source: str) -> bool | None:
        try:
            d = await self.request("GetSourceActive", {"sourceName": source})
            return bool(d.get("videoActive"))
        except OBSError:
            return None

    async def screenshot(self, source: str, width: int | None = None, quality: int = 90) -> bytes:
        """JPEG (or PNG fallback) bytes of one input source, without the overlay."""
        fmt = "jpg" if ("jpg" in self.image_formats or not self.image_formats) else (
            "jpeg" if "jpeg" in self.image_formats else "png"
        )
        req: dict = {"sourceName": source, "imageFormat": fmt, "imageCompressionQuality": int(quality)}
        if width:
            req["imageWidth"] = max(8, min(4096, int(width)))
        data = await self.request("GetSourceScreenshot", req)
        raw = data.get("imageData")
        if not isinstance(raw, str) or not raw:
            raise OBSError("OBS returned no image data")
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            return base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            raise OBSError("OBS returned undecodable image data") from None

    # -- connection management ----------------------------------------------------------
    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            if state == "connected":
                self.connected_since = time.time()
                self._connected_evt.set()
            else:
                self.connected_since = None
                self._connected_evt.clear()
            for cb in list(self._listeners):
                try:
                    cb(state)
                except Exception:  # pragma: no cover
                    log.exception("OBS state listener failed")

    async def _run(self) -> None:
        backoff = 1.0
        first = True
        while not self._stopping:
            self._wake.clear()
            if not first:
                self.reconnects += 1
            first = False
            auth_failed = False
            try:
                await self._connect_once()
                backoff = 1.0
            except OBSAuthError as e:
                auth_failed = True
                self.last_error = str(e)
                self._set_state("auth_failed")
                log.error("OBS authentication failed — check the OBS WebSocket password")
            except (OSError, ConnectionClosed, websockets.InvalidHandshake, websockets.InvalidURI, OBSError, asyncio.TimeoutError) as e:
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                self._set_state("disconnected")
                log.info("OBS connection unavailable (%s); retrying in %.0fs", type(e).__name__, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - defensive
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                self._set_state("disconnected")
                log.exception("unexpected OBS connection error")
            finally:
                self._fail_pending()
                self._ws = None
            if self._stopping:
                break
            delay = 30.0 if auth_failed else backoff * random.uniform(0.85, 1.15)
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(self._max_backoff, backoff * 2)

    async def _connect_once(self) -> None:
        host, port = self._address()
        url = f"ws://{host}:{port}"
        self._set_state("connecting")
        async with connect(
            url,
            subprotocols=["obswebsocket.json"],
            max_size=64 * 1024 * 1024,
            open_timeout=5,
            ping_interval=10,
            ping_timeout=10,
            close_timeout=2,
        ) as ws:
            hello = await asyncio.wait_for(_recv_json(ws), 5)
            if hello.get("op") != 0:
                raise OBSError("OBS did not send Hello")
            hd = hello.get("d") or {}
            identify: dict = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = hd.get("authentication")
            if auth:
                pw = self._password()
                if not pw:
                    raise OBSAuthError("OBS requires a password but none is configured")
                identify["authentication"] = compute_auth(pw, auth["salt"], auth["challenge"])
            await ws.send(json.dumps({"op": 1, "d": identify}))
            try:
                ident = await asyncio.wait_for(_recv_json(ws), 5)
            except ConnectionClosed as e:
                if e.rcvd and e.rcvd.code == AUTH_FAILED_CLOSE_CODE:
                    raise OBSAuthError("OBS rejected the WebSocket password") from None
                raise
            if ident.get("op") != 2:
                raise OBSError("OBS did not acknowledge Identify")
            self._ws = ws
            self.last_error = None
            self._set_state("connected")
            log.info("connected to OBS at %s", url)
            reader = asyncio.create_task(self._reader(ws))
            try:
                ver = await self.request("GetVersion")
                self.version = ver
                self.image_formats = [str(f).lower() for f in ver.get("supportedImageFormats") or []]
                await reader
            finally:
                reader.cancel()
                if self._ws is ws:
                    self._ws = None
                self._set_state("disconnected")

    async def _reader(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or msg.get("op") != 7:
                    continue
                d = msg.get("d") or {}
                fut = self._pending.get(d.get("requestId"))
                if fut is not None and not fut.done():
                    fut.set_result(d)
        except ConnectionClosed:
            pass
        finally:
            # Fail in-flight requests immediately instead of letting them time out.
            if self._ws is ws:
                self._ws = None
                self._set_state("disconnected")
            self._fail_pending()

    def _fail_pending(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(OBSNotConnected("OBS connection lost"))
                fut.exception()
        self._pending.clear()


async def _recv_json(ws: ClientConnection) -> dict:
    raw = await ws.recv()
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        raise OBSError("unexpected message from OBS")
    return msg
