"""Public overlay state and the read-only overlay WebSocket.

This hub is the *only* path by which game data reaches OBS Browser Sources:

* It holds at most one :class:`VerifiedRecord` — title, platform and the three
  prices of one PriceCharting product, swapped atomically as a single object.
* Demo records are rejected unless the process runs in demo mode.
* Clients receive full-state snapshots (never partial updates), so a client can
  never combine a new title with old prices, even after missed messages.
* Messages from overlay clients are read and discarded (the overlay is read-only).
* Payloads contain no secrets, admin routes, recognition reasoning or confidence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect

from ..config import DisplaySettings, OverlaySettings
from ..models import VerifiedRecord

log = logging.getLogger(__name__)

HEARTBEAT_S = 10.0
SEND_TIMEOUT_S = 2.0
MAX_CLIENTS = 32


class DemoDataRejected(Exception):
    pass


def overlay_build() -> str:
    """Short fingerprint of the overlay's files (changes whenever the overlay is updated)."""
    import hashlib
    from importlib import resources

    h = hashlib.sha256()
    root = resources.files("gameoverlay").joinpath("web", "overlay")
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.is_file() and entry.name.endswith((".html", ".css", ".js", ".png", ".ttf")):
            h.update(entry.name.encode())
            h.update(entry.read_bytes())
    return h.hexdigest()[:12]


class OverlayHub:
    def __init__(
        self,
        demo_mode: bool,
        overlay_settings: Callable[[], OverlaySettings],
        display_settings: Callable[[], DisplaySettings],
        build: str | None = None,
    ) -> None:
        self.demo_mode = demo_mode
        # Identifies the overlay page version. OBS keeps an already-open Browser Source page
        # running across app updates; pages from an older build reload themselves.
        self.build = build or overlay_build()
        self._overlay_settings = overlay_settings
        self._display_settings = display_settings
        self.current: VerifiedRecord | None = None
        self.visible = False
        self.seq = 0
        self.shown_at: float | None = None
        self.hidden_reason: str | None = "startup"
        self._clients: set[WebSocket] = set()
        self._dirty = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._listeners: list[Callable[[], None]] = []
        self.broadcasts = 0

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._broadcaster(), name="overlay-broadcaster")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for ws in list(self._clients):
            try:
                await ws.close(1001)
            except Exception:
                pass
        self._clients.clear()

    def on_change(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # -- state mutation (atomic) ---------------------------------------------------------
    def show(self, record: VerifiedRecord) -> None:
        if not isinstance(record, VerifiedRecord):
            raise TypeError("only VerifiedRecord instances can be displayed")
        if record.demo and not self.demo_mode:
            raise DemoDataRejected("demo records cannot be shown in live mode")
        if not self.demo_mode and record.source == "demo":
            raise DemoDataRejected("demo-sourced records cannot be shown in live mode")
        if not record.title or not record.platform or not record.product_id:
            raise ValueError("incomplete record")
        self.current = record
        self.visible = True
        self.shown_at = time.time()
        self.hidden_reason = None
        self._changed()

    def hide(self, reason: str) -> None:
        if not self.visible and self.current is None:
            self.hidden_reason = reason
            return
        self.visible = False
        self.current = None
        self.shown_at = None
        self.hidden_reason = reason
        self._changed()

    def layout_changed(self) -> None:
        self._changed()

    def _changed(self) -> None:
        self.seq += 1
        self._dirty.set()
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # pragma: no cover
                log.exception("overlay listener failed")

    # -- payloads ------------------------------------------------------------------------
    def layout(self) -> dict:
        o = self._overlay_settings()
        return {
            "size": o.size,
            "position": o.position,
            "orientation": o.orientation,
            "opacity": o.opacity,
            "fontScale": o.font_scale,
            "edgeOffset": o.edge_offset,
            "attribution": o.attribution_text,
            "showAttribution": o.show_attribution,
            "showRetrievalTime": o.show_retrieval_time,
            "idlePanel": o.idle_panel,
            "roundWholeDollars": o.round_whole_dollars,
            "idleTitle": o.idle_title,
            "showProductLink": o.show_product_link,
            "reducedMotion": o.reduced_motion,
            "disconnectHideMs": int(self._display_settings().overlay_disconnect_hide_s * 1000),
        }

    def snapshot(self) -> dict:
        rec = self.current.to_overlay_dict() if (self.visible and self.current) else None
        return {
            "type": "state",
            "seq": self.seq,
            "visible": rec is not None,
            "record": rec,
            "layout": self.layout(),
            "demoMode": self.demo_mode,
            "build": self.build,
            "serverTime": time.time(),
        }

    # -- websocket -------------------------------------------------------------------------
    async def handle_client(self, ws: WebSocket, allowed_origins: set[str] | None = None) -> None:
        origin = ws.headers.get("origin")
        if allowed_origins is not None and origin and origin not in allowed_origins:
            await ws.close(1008)  # other web pages in the streamer's browser may not hog slots
            return
        if len(self._clients) >= MAX_CLIENTS:
            await ws.close(1013)
            return
        await ws.accept()
        self._clients.add(ws)
        try:
            await self._send(ws, self.snapshot())
            hb = asyncio.create_task(self._heartbeat(ws))
            try:
                while True:
                    # Read and discard: the overlay is strictly read-only.
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
            finally:
                hb.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self._clients.discard(ws)

    async def _heartbeat(self, ws: WebSocket) -> None:
        try:
            while ws in self._clients:
                await asyncio.sleep(HEARTBEAT_S)
                if ws not in self._clients:
                    break
                await self._send(ws, {"type": "ping", "seq": self.seq, "serverTime": time.time()})
        except Exception:
            await self._evict(ws)

    async def _evict(self, ws: WebSocket) -> None:
        """Drop a client that missed an update and CLOSE it, so the overlay reconnects and
        receives the full current state (instead of silently keeping an old record)."""
        self._clients.discard(ws)
        try:
            await asyncio.wait_for(ws.close(1011), 1.0)
        except Exception:
            pass

    async def _send(self, ws: WebSocket, payload: dict) -> None:
        await asyncio.wait_for(ws.send_text(json.dumps(payload)), SEND_TIMEOUT_S)

    async def _broadcaster(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            payload = self.snapshot()  # always the latest full state
            dead = []
            for ws in list(self._clients):
                try:
                    await self._send(ws, payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                await self._evict(ws)
            self.broadcasts += 1
