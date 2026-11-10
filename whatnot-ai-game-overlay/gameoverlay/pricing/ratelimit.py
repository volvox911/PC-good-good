"""Centralized, serialized request spacing for the PriceCharting API.

PriceCharting allows at most one call per second and may revoke access if that is
exceeded. Every PriceCharting request in the process goes through one
:class:`SerialRateLimiter`:

* requests run strictly one at a time (FIFO, via an ``asyncio.Lock``);
* each request *starts* at least ``min_interval`` seconds after the previous one
  started (and never before the previous one finished);
* ``penalize()`` pushes the next allowed start further out (``Retry-After``/429).

``min_interval`` can never be configured below 1.0 s.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

HARD_MIN_INTERVAL_S = 1.0


class SerialRateLimiter:
    def __init__(
        self,
        min_interval: float = 1.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = max(HARD_MIN_INTERVAL_S, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0
        self.starts: list[float] = []  # recent start times (bounded), for diagnostics/tests
        self.total = 0

    @property
    def interval(self) -> float:
        return self._interval

    def set_interval(self, seconds: float) -> None:
        self._interval = max(HARD_MIN_INTERVAL_S, float(seconds))

    def penalize(self, seconds: float) -> None:
        self._next_allowed = max(self._next_allowed, self._clock() + max(0.0, seconds))

    @property
    def queue_depth(self) -> int:
        waiters = getattr(self._lock, "_waiters", None)
        return (len(waiters) if waiters else 0) + (1 if self._lock.locked() else 0)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0:
                await self._sleep(wait)
            start = self._clock()
            self._next_allowed = start + self._interval
            self.total += 1
            self.starts.append(start)
            if len(self.starts) > 200:
                del self.starts[:100]
            try:
                yield
            finally:
                # Never start the next call before this one has finished.
                self._next_allowed = max(self._next_allowed, self._clock() + 0.05)
