"""Demo 'camera world': a scripted seller presenting one game after another."""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from .scene import DEMO_ITEMS, DemoItem, render_scene, to_jpeg


@dataclass(frozen=True)
class Segment:
    item: DemoItem | None
    duration: float
    moving: bool


class DemoWorld:
    def __init__(
        self,
        items: list[DemoItem] | None = None,
        hold_s: float = 10.0,
        transition_s: float = 1.6,
        empty_every: int = 4,
        empty_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.items = items or DEMO_ITEMS
        self.clock = clock
        self.t0 = clock()
        segs: list[Segment] = [Segment(None, 2.0, False)]
        for i, item in enumerate(self.items):
            segs.append(Segment(item, transition_s, True))
            segs.append(Segment(item, hold_s, False))
            if empty_every and (i + 1) % empty_every == 0:
                segs.append(Segment(None, transition_s, True))
                segs.append(Segment(None, empty_s, False))
        self.segments = segs
        self.period = sum(s.duration for s in segs)
        self.paused_item: DemoItem | None | bool = False  # False = scripted

    def _segment(self) -> tuple[Segment, float]:
        t = (self.clock() - self.t0) % self.period
        for s in self.segments:
            if t < s.duration:
                return s, t
            t -= s.duration
        return self.segments[-1], 0.0

    def current_item(self) -> DemoItem | None:
        if self.paused_item is not False:
            return self.paused_item  # type: ignore[return-value]
        return self._segment()[0].item

    def frame(self, source: str, width: int | None, quality: int) -> bytes:
        if self.paused_item is not False:
            seg, t = Segment(self.paused_item, 1.0, False), 0.0  # type: ignore[arg-type]
        else:
            seg, t = self._segment()
        jitter = 1.0 if seg.moving else 0.0
        return _frame_cached(seg.item, round(t * 10) / 10 if seg.moving else 0.0, jitter, width or 1280, quality)


@lru_cache(maxsize=64)
def _frame_cached(item: DemoItem | None, t: float, jitter: float, width: int, quality: int) -> bytes:
    return to_jpeg(render_scene(item, t=t, jitter=jitter), width=width, quality=quality)
