"""A mock of the documented PriceCharting endpoints as an ``httpx.MockTransport``.

Used by demo mode and the test-suite so the *real* :class:`PriceChartingClient`
(rate limiting, retries, parsing) is exercised without paid API calls.
DEMO DATA ONLY — the catalog contains illustrative values.
"""

from __future__ import annotations

import asyncio
import json
import re
from importlib import resources
from typing import Callable

import httpx

DEMO_TOKEN = "demo-token-not-a-real-pricecharting-token-00"


def load_catalog() -> list[dict]:
    text = resources.files("gameoverlay.fixtures").joinpath("demo_catalog.json").read_text("utf-8")
    return json.loads(text)["products"]


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if t}


class MockPriceCharting:
    """Configurable fake. Tweak ``fail_next``/``delay``/``token`` in tests."""

    def __init__(self, catalog: list[dict] | None = None, token: str = DEMO_TOKEN) -> None:
        self.catalog = {p["id"]: dict(p) for p in (catalog or load_catalog())}
        self.token = token
        self.calls: list[tuple[str, dict]] = []
        self.call_times: list[float] = []
        self.fail_next: list[int | str] = []  # HTTP codes or "timeout"/"badjson"/"autherr"
        self.delay_s: float | Callable[[str, dict], float] = 0.0
        self.clock: Callable[[], float] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        path = request.url.path
        self.calls.append((path, {k: v for k, v in params.items() if k != "t"}))
        if self.clock:
            self.call_times.append(self.clock())
        delay = self.delay_s(path, params) if callable(self.delay_s) else self.delay_s
        if delay:
            await asyncio.sleep(delay)
        if self.fail_next:
            f = self.fail_next.pop(0)
            if f == "timeout":
                raise httpx.ReadTimeout("mock timeout", request=request)
            if f == "badjson":
                return httpx.Response(200, text="<html>oops</html>")
            if f == "autherr":
                return httpx.Response(200, json={"status": "error", "error-message": "Invalid access token"})
            if f == 429:
                return httpx.Response(429, headers={"Retry-After": "1"}, json={"status": "error", "error-message": "Too many requests"})
            return httpx.Response(int(f), json={"status": "error", "error-message": f"HTTP {f}"})
        if params.get("t") != self.token:
            return httpx.Response(401, json={"status": "error", "error-message": "Invalid or missing API token"})
        if path == "/api/products":
            return httpx.Response(200, json={"status": "success", "products": self._search(params.get("q", ""))})
        if path == "/api/product":
            if "id" in params:
                p = self.catalog.get(params["id"])
            elif "upc" in params:
                p = next((x for x in self.catalog.values() if x.get("upc") == params["upc"]), None)
            elif "q" in params:
                res = self._search(params["q"])
                p = self.catalog.get(res[0]["id"]) if res else None
            else:
                p = None
            if p is None:
                return httpx.Response(404, json={"status": "error", "error-message": "No product found"})
            return httpx.Response(200, json=p)
        return httpx.Response(404, json={"status": "error", "error-message": "unknown API"})

    def _search(self, q: str) -> list[dict]:
        qt = _tokens(q)
        scored = []
        for p in self.catalog.values():
            pt = _tokens(p["product-name"]) | _tokens(p["console-name"])
            overlap = len(qt & pt)
            if overlap:
                scored.append((overlap, -len(pt), p))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [
            {"id": p["id"], "product-name": p["product-name"], "console-name": p["console-name"]}
            for _, _, p in scored[:20]
        ]
