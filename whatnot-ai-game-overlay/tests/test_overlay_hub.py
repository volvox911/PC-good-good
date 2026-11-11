"""Overlay hub: atomic snapshots, eviction closes stalled clients (review #4)."""

from __future__ import annotations

import asyncio
import json

from gameoverlay.config import DisplaySettings, OverlaySettings
from gameoverlay.models import VerifiedRecord
from gameoverlay.overlay import hub as hub_mod
from gameoverlay.overlay.hub import OverlayHub


class FakeWS:
    def __init__(self, stall: bool = False) -> None:
        self.stall = stall
        self.sent: list[dict] = []
        self.closed = False
        self.headers = {}

    async def send_text(self, text: str) -> None:
        if self.stall:
            await asyncio.sleep(60)
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed = True


def rec(pid="1", title="A"):
    return VerifiedRecord(title=title, platform="NES", product_id=pid, loose_cents=100, cib_cents=None,
                          new_cents=None, retrieved_at=0, freshness="fresh", source="auto")


async def test_stalled_client_is_closed_so_it_reconnects(monkeypatch):
    monkeypatch.setattr(hub_mod, "SEND_TIMEOUT_S", 0.05)
    h = OverlayHub(False, lambda: OverlaySettings(), lambda: DisplaySettings())
    h.start()
    good, bad = FakeWS(), FakeWS(stall=True)
    h._clients |= {good, bad}
    h.show(rec())
    await asyncio.sleep(0.3)
    assert bad.closed, "a client that missed an update must be closed (it then resyncs)"
    assert bad not in h._clients and good in h._clients
    assert good.sent[-1]["record"]["productId"] == "1"
    await h.stop()


async def test_snapshot_is_full_state():
    h = OverlayHub(False, lambda: OverlaySettings(), lambda: DisplaySettings())
    h.show(rec("1", "A"))
    h.show(rec("2", "B"))
    snap = h.snapshot()
    assert snap["visible"] and snap["record"]["productId"] == "2" and snap["record"]["title"] == "B"
    h.hide("x")
    assert h.snapshot()["record"] is None



async def test_title_only_snapshot_carries_no_prices():
    settings = OverlaySettings(show_prices=False)
    h = OverlayHub(False, lambda: settings, lambda: DisplaySettings())
    h.show(rec("7", "Game"))
    snap = h.snapshot()
    assert snap["layout"]["showPrices"] is False
    r = snap["record"]
    assert r["title"] == "Game" and r["platform"] == "NES" and r["pricesHidden"] is True
    assert all(r[k] is None for k in ("looseUsd", "completeUsd", "newUsd", "looseCents", "completeCents", "newCents"))
    assert h.current.loose_cents == 100, "the record itself (operator's view) keeps its prices"
    settings = OverlaySettings()  # turned back on
    assert h.snapshot()["record"]["looseCents"] == 100 and "pricesHidden" not in h.snapshot()["record"]
