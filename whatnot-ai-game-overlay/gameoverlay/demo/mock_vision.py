"""Mock 'Claude Vision' for demo mode and tests. Never used in live mode."""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from ..vision.recognizer import RecognitionOutcome
from ..vision.schema import GameIdentification
from .scene import DemoItem


def identification_for(item: DemoItem | None, **overrides) -> GameIdentification:
    if item is None:
        data = dict(
            item_present=False, item_type="none", title=None, alternate_titles=[], platform=None,
            platform_evidence="", region="unknown", region_evidence=None, edition=None,
            edition_evidence=None, media_format="unknown", visible_text=[], upc_digits=None,
            title_obscured=False, confidence="high", needs_more_images=False,
        )
    else:
        data = dict(
            item_present=True, item_type=item.item_type, title=item.label_title.title(), alternate_titles=[],
            platform=item.platform, platform_evidence=f"label reads {item.label_platform}", region=item.region,
            region_evidence=None, edition=item.edition, edition_evidence=item.edition,
            media_format="boxed_game", visible_text=[item.label_title, item.label_platform], upc_digits=None,
            title_obscured=False, confidence=item.confidence, needs_more_images=False,
        )
    data.update(overrides)
    return GameIdentification.model_validate(data)


class MockRecognizer:
    """Returns the identification of whatever the demo camera is currently showing."""

    def __init__(self, current_item: Callable[[], DemoItem | None], latency_s: float = 0.9) -> None:
        self._current = current_item
        self.latency_s = latency_s
        self.calls = 0
        self.last_ok_at: float | None = None

    async def recognize(self, images: list[bytes]) -> RecognitionOutcome:
        self.calls += 1
        item = self._current()
        await asyncio.sleep(self.latency_s)
        self.last_ok_at = time.time()
        return RecognitionOutcome("ok", result=identification_for(item), model="demo-mock", latency_ms=int(self.latency_s * 1000), images=len(images))

    def status(self) -> dict:
        return {
            "state": "demo",
            "lastOkAt": self.last_ok_at,
            "lastError": None,
            "lastLatencyMs": int(self.latency_s * 1000),
            "model": "demo-mock (no API calls)",
            "usage": {"requests": self.calls, "errors": 0, "inputTokens": 0, "outputTokens": 0,
                      "estimatedCostUsd": 0.0, "limitUsd": 0.0, "limitReached": False},
        }


class ScriptedRecognizer:
    """Test double: returns queued outcomes (or a function of the call index)."""

    def __init__(self, outcomes: list[RecognitionOutcome] | Callable[[int, list[bytes]], RecognitionOutcome], delay_s: float = 0.0):
        self._outcomes = outcomes
        self.delay_s = delay_s
        self.calls: list[int] = []

    async def recognize(self, images: list[bytes]) -> RecognitionOutcome:
        idx = len(self.calls)
        self.calls.append(len(images))
        d = self.delay_s(idx) if callable(self.delay_s) else self.delay_s
        if d:
            await asyncio.sleep(d)
        if callable(self._outcomes):
            return self._outcomes(idx, images)
        return self._outcomes[min(idx, len(self._outcomes) - 1)]

    def status(self) -> dict:
        return {"state": "test", "lastOkAt": None, "lastError": None, "lastLatencyMs": None, "model": "scripted",
                "usage": {"requests": len(self.calls)}}
