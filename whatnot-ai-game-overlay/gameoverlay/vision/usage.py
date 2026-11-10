"""Claude API usage accounting, cost estimates and per-session spend limits.

Costs are *estimates* computed from ``response.usage`` and the public per-token price
list below; your Anthropic invoice is authoritative.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from ..config import RecognitionSettings
from ..db import Database

# USD per 1M tokens: (input, output, cache_read). Cache writes billed at 1.25x input.
PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
DEFAULT_PRICE = (8.0, 40.0, 0.80)  # unknown model: assume the most expensive tier

# Conservative worst-case cost of one identification request, used *before* calling.
PROJECTED_REQUEST_COST_USD = 0.08


class SpendLimitReached(Exception):
    pass


class RequestRateExceeded(Exception):
    pass


@dataclass
class UsageSnapshot:
    requests: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    limit_usd: float
    limit_reached: bool

    def to_dict(self) -> dict:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "estimatedCostUsd": round(self.cost_usd, 4),
            "limitUsd": self.limit_usd,
            "limitReached": self.limit_reached,
        }


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    pin, pout, pcache = PRICES_PER_MTOK.get(model, DEFAULT_PRICE)
    return (
        input_tokens * pin + output_tokens * pout + cache_read * pcache + cache_write * pin * 1.25
    ) / 1_000_000


class UsageTracker:
    def __init__(
        self,
        db: Database,
        session_id: str,
        settings: Callable[[], RecognitionSettings],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.session_id = session_id
        self._settings = settings
        self._clock = clock
        self._recent: deque[float] = deque()
        self._limit_override = False

    def snapshot(self) -> UsageSnapshot:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS e, "
            "COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o, "
            "COALESCE(SUM(cost_usd),0) AS c FROM api_usage WHERE session_id=? AND provider='anthropic'",
            (self.session_id,),
        )
        limit = self._settings().session_spend_limit_usd
        cost = float(row["c"] or 0)
        return UsageSnapshot(
            requests=int(row["n"] or 0),
            errors=int(row["e"] or 0),
            input_tokens=int(row["i"] or 0),
            output_tokens=int(row["o"] or 0),
            cost_usd=cost,
            limit_usd=limit,
            limit_reached=limit > 0 and cost + PROJECTED_REQUEST_COST_USD > limit,
        )

    def check_allowed(self) -> None:
        cfg = self._settings()
        snap = self.snapshot()
        if cfg.session_spend_limit_usd <= 0 or snap.limit_reached:
            raise SpendLimitReached(
                f"Session Claude spend limit reached (est. ${snap.cost_usd:.2f} of ${cfg.session_spend_limit_usd:.2f})"
            )
        now = self._clock()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= cfg.max_requests_per_minute:
            raise RequestRateExceeded("Claude request rate limit (per minute) reached")
        self._recent.append(now)

    def record(
        self,
        model: str,
        status: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> float:
        cost = estimate_cost(model, input_tokens, output_tokens, cache_read, cache_write)
        self.db.execute(
            "INSERT INTO api_usage(session_id, provider, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, cost_usd, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, "anthropic", model, input_tokens, output_tokens, cache_read, cache_write, cost, status, self._clock()),
        )
        return cost
