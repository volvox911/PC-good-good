"""Price + search caching on top of :class:`PriceChartingClient`.

Freshness policy
----------------
* ``fresh``  — retrieved from the API during this lookup.
* ``cached`` — from the local cache and younger than ``price_refresh_s`` (default 1 h).
* ``stale``  — older than ``price_refresh_s`` and the refresh attempt failed.

Stale data is never presented silently as current: with ``stale_policy="hide"``
(default) a failed refresh raises :class:`PriceUnavailable`; with
``stale_policy="show_labeled"`` the stale price may be shown but is labeled
``CACHED`` on the overlay and in the dashboard, and only up to ``max_stale_s``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from ..config import PricingSettings
from ..db import Database
from ..models import Candidate, PriceLookup, ProductPrices
from .client import PriceChartingClient, PriceChartingError, PriceChartingNotFound

log = logging.getLogger(__name__)


class PriceUnavailable(Exception):
    def __init__(self, message: str, stale: ProductPrices | None = None) -> None:
        super().__init__(message)
        self.stale = stale


class PricingService:
    def __init__(
        self,
        client: PriceChartingClient,
        db: Database,
        settings: Callable[[], PricingSettings],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.db = db
        self._settings = settings
        self._clock = clock
        self._inflight: dict[str, asyncio.Future] = {}

    # -- prices --------------------------------------------------------------------------------
    def cached_prices(self, product_id: str) -> ProductPrices | None:
        row = self.db.query_one("SELECT * FROM price_cache WHERE product_id=?", (str(product_id),))
        if row is None:
            return None
        return ProductPrices(
            product_id=row["product_id"],
            product_name=row["product_name"],
            console_name=row["console_name"],
            loose_cents=row["loose_cents"],
            cib_cents=row["cib_cents"],
            new_cents=row["new_cents"],
            retrieved_at=row["retrieved_at"],
            upc=row["upc"],
            release_date=row["release_date"],
        )

    def classify(self, prices: ProductPrices) -> str:
        return "cached" if prices.age_s(self._clock()) < self._settings().price_refresh_s else "stale"

    def store_prices(self, p: ProductPrices) -> None:
        self.db.execute(
            "INSERT INTO price_cache(product_id, product_name, console_name, loose_cents, cib_cents, "
            "new_cents, upc, release_date, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(product_id) DO UPDATE SET product_name=excluded.product_name, "
            "console_name=excluded.console_name, loose_cents=excluded.loose_cents, "
            "cib_cents=excluded.cib_cents, new_cents=excluded.new_cents, upc=excluded.upc, "
            "release_date=excluded.release_date, retrieved_at=excluded.retrieved_at",
            (
                p.product_id,
                p.product_name,
                p.console_name,
                p.loose_cents,
                p.cib_cents,
                p.new_cents,
                p.upc,
                p.release_date,
                p.retrieved_at,
            ),
        )

    async def get_prices(self, product_id: str, force_refresh: bool = False) -> PriceLookup:
        pid = str(product_id)
        cfg = self._settings()
        cached = self.cached_prices(pid)
        if cached is not None and not force_refresh and cached.age_s(self._clock()) < cfg.price_refresh_s:
            return PriceLookup(cached, "cached", refreshed=False)
        try:
            fresh = await self._fetch_dedup(pid)
        except PriceChartingError as e:
            if (
                cached is not None
                and cfg.stale_policy == "show_labeled"
                and cached.age_s(self._clock()) < cfg.max_stale_s
            ):
                log.warning("price refresh failed for %s; showing labeled stale cache: %s", pid, e)
                return PriceLookup(cached, "stale", refreshed=False)
            raise PriceUnavailable(f"price refresh failed: {e}", stale=cached) from e
        self.store_prices(fresh)
        return PriceLookup(fresh, "fresh", refreshed=True)

    async def _fetch_dedup(self, pid: str) -> ProductPrices:
        fut = self._inflight.get(pid)
        if fut is not None:
            return await asyncio.shield(fut)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[pid] = fut
        try:
            result = await self.client.product(pid)
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e if isinstance(e, Exception) else PriceChartingError("cancelled"))
                fut.exception()  # mark retrieved
            raise
        else:
            fut.set_result(result)
            return result
        finally:
            self._inflight.pop(pid, None)

    async def lookup_upc(self, upc: str) -> ProductPrices | None:
        p = await self.client.product_by_upc(upc)
        if p is not None:
            self.store_prices(p)
        return p

    # -- search --------------------------------------------------------------------------------
    async def search(self, query: str, use_cache: bool = True) -> list[Candidate]:
        key = " ".join(query.lower().split())
        if not key:
            return []
        ttl = self._settings().search_cache_s
        if use_cache and ttl > 0:
            row = self.db.query_one("SELECT results_json, retrieved_at FROM search_cache WHERE query=?", (key,))
            if row is not None and self._clock() - row["retrieved_at"] < ttl:
                try:
                    return [Candidate(**c) for c in json.loads(row["results_json"])]
                except (ValueError, TypeError):
                    pass
        try:
            results = await self.client.search(query)
        except PriceChartingNotFound:
            results = []
        self.db.execute(
            "INSERT INTO search_cache(query, results_json, retrieved_at) VALUES (?,?,?) "
            "ON CONFLICT(query) DO UPDATE SET results_json=excluded.results_json, retrieved_at=excluded.retrieved_at",
            (
                key,
                json.dumps(
                    [
                        {"product_id": c.product_id, "product_name": c.product_name, "console_name": c.console_name}
                        for c in results
                    ]
                ),
                self._clock(),
            ),
        )
        return results

    def local_suggestions(self, query: str, limit: int = 8) -> list[Candidate]:
        """Instant, API-free suggestions from previously seen products."""
        words = [w for w in query.lower().split() if w]
        if not words:
            return []
        sql = "SELECT product_id, product_name, console_name FROM price_cache WHERE " + " AND ".join(
            ["(lower(product_name) LIKE ? OR lower(console_name) LIKE ?)"] * len(words)
        )
        params: list[str] = []
        for w in words:
            params += [f"%{w}%", f"%{w}%"]
        rows = self.db.query(sql + " ORDER BY retrieved_at DESC LIMIT ?", (*params, limit))
        return [Candidate(r["product_id"], r["product_name"], r["console_name"]) for r in rows]
