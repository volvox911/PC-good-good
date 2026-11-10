"""In-process fake of the obs-websocket v5 server (tests and demo mode).

Implements Hello/Identify (with password auth), GetVersion, GetInputList,
GetSourceActive and GetSourceScreenshot. Any other request type is answered with
``UnknownRequestType`` — and is recorded, so tests can assert the real client never
sends anything that could change OBS state.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from typing import Awaitable, Callable

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from ..obs.client import compute_auth

FrameProvider = Callable[[str, int | None, int], bytes | Awaitable[bytes]]

DEFAULT_INPUTS = [
    {"inputName": "Sony ZV-1", "inputKind": "dshow_input", "unversionedInputKind": "dshow_input"},
    {"inputName": "Game Overlay", "inputKind": "browser_source", "unversionedInputKind": "browser_source"},
    {"inputName": "Mic/Aux", "inputKind": "wasapi_input_capture", "unversionedInputKind": "wasapi_input_capture"},
]


class FakeOBSServer:
    def __init__(
        self,
        frame_provider: FrameProvider,
        password: str | None = None,
        inputs: list[dict] | None = None,
    ) -> None:
        self.frame_provider = frame_provider
        self.password = password
        self.inputs = inputs if inputs is not None else list(DEFAULT_INPUTS)
        self.requests_seen: list[str] = []
        self.connections: set[ServerConnection] = set()
        self._server: Server | None = None
        self.port: int | None = None
        self.screenshot_delay_s = 0.0
        self.auth_failures = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await serve(
            self._handler, host, port, subprotocols=["obswebsocket.json"], max_size=64 * 1024 * 1024
        )
        self.port = self._server.sockets[0].getsockname()[1] if port == 0 else port
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop_all(self) -> None:
        """Simulate OBS crashing: abort every client connection."""
        for c in list(self.connections):
            try:
                c.transport.abort()
            except Exception:
                pass

    async def _handler(self, ws: ServerConnection) -> None:
        self.connections.add(ws)
        try:
            hello: dict = {"obsWebSocketVersion": "5.5.4", "rpcVersion": 1}
            salt = challenge = None
            if self.password:
                salt, challenge = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
                hello["authentication"] = {"salt": salt, "challenge": challenge}
            await ws.send(json.dumps({"op": 0, "d": hello}))
            ident = json.loads(await ws.recv())
            if ident.get("op") != 1:
                await ws.close(4007, "not identified")
                return
            if self.password:
                expected = compute_auth(self.password, salt, challenge)
                if (ident.get("d") or {}).get("authentication") != expected:
                    self.auth_failures += 1
                    await ws.close(4009, "Authentication failed.")
                    return
            await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("op") != 6:
                    continue
                d = msg["d"]
                rtype = d.get("requestType")
                self.requests_seen.append(rtype)
                asyncio.create_task(self._respond(ws, rtype, d.get("requestId"), d.get("requestData") or {}))
        except ConnectionClosed:
            pass
        finally:
            self.connections.discard(ws)

    async def _respond(self, ws: ServerConnection, rtype: str, rid: str, data: dict) -> None:
        status = {"result": True, "code": 100}
        resp: dict = {}
        try:
            if rtype == "GetVersion":
                resp = {
                    "obsVersion": "31.0.0",
                    "obsWebSocketVersion": "5.5.4",
                    "rpcVersion": 1,
                    "supportedImageFormats": ["bmp", "jpeg", "jpg", "png"],
                }
            elif rtype == "GetInputList":
                resp = {"inputs": self.inputs}
            elif rtype == "GetSourceActive":
                known = any(i["inputName"] == data.get("sourceName") for i in self.inputs)
                if not known:
                    status = {"result": False, "code": 600, "comment": "No source was found"}
                resp = {"videoActive": known, "videoShowing": known}
            elif rtype == "GetSourceScreenshot":
                name = data.get("sourceName")
                if not any(i["inputName"] == name for i in self.inputs):
                    status = {"result": False, "code": 600, "comment": f"No source was found by the name of `{name}`."}
                else:
                    if self.screenshot_delay_s:
                        await asyncio.sleep(self.screenshot_delay_s)
                    img = self.frame_provider(name, data.get("imageWidth"), int(data.get("imageCompressionQuality", 90)))
                    if asyncio.iscoroutine(img):
                        img = await img
                    resp = {"imageData": "data:image/jpg;base64," + base64.b64encode(img).decode()}
            else:
                status = {"result": False, "code": 204, "comment": "Your request type is not valid."}
            await ws.send(
                json.dumps(
                    {"op": 7, "d": {"requestType": rtype, "requestId": rid, "requestStatus": status, "responseData": resp}}
                )
            )
        except ConnectionClosed:
            pass
