"""PriceCharting Prices API client.

Documented endpoints used (https://www.pricecharting.com/api-documentation):

* ``GET /api/products?t=TOKEN&q=QUERY`` — up to 20 matching products
  (``id``, ``product-name``, ``console-name``).
* ``GET /api/product?t=TOKEN&id=ID`` — one product with prices in integer pennies.
* ``GET /api/product?t=TOKEN&upc=UPC`` — product by UPC.

Field mapping (exact, never substituted):
``loose-price`` -> LOOSE, ``cib-price`` -> COMPLETE, ``new-price`` -> NEW.
Retail buy/sell, graded and box/manual-only prices are deliberately ignored.

The API token is sent only as the ``t`` query parameter to pricecharting.com and is
never logged, returned to browser clients, or included in exceptions.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Callable

import httpx

from ..models import Candidate, ProductPrices
from ..money import parse_cents
from .ratelimit import SerialRateLimiter

log = logging.getLogger(__name__)


class _TokenScrubFilter(logging.Filter):
    """httpx logs full request URLs (including ``?t=TOKEN``). Scrub at the source."""

    _pat = re.compile(r"([?&]t=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = self._pat.sub(r"\1[REDACTED]", msg)
        record.args = None
        return True


for _name in ("httpx", "httpcore"):
    _lg = logging.getLogger(_name)
    _lg.addFilter(_TokenScrubFilter())
    if _lg.level == logging.NOTSET or _lg.level < logging.WARNING:
        _lg.setLevel(logging.WARNING)

BASE_URL = "https://www.pricecharting.com"
USER_AGENT = "whatnot-ai-game-overlay/1.0 (+local OBS overlay)"


class PriceChartingError(Exception):
    """Base error. Messages never contain the API token."""


class PriceChartingAuthError(PriceChartingError):
    pass


class PriceChartingRateLimited(PriceChartingError):
    pass


class PriceChartingUnavailable(PriceChartingError):
    pass


class PriceChartingNotFound(PriceChartingError):
    pass


class PriceChartingBadResponse(PriceChartingError):
    pass


_AUTH_HINTS = ("token", "auth", "subscription", "access", "permission", "invalid t")
_NOT_FOUND_HINTS = ("not found", "no product", "no such", "unknown product", "no match")


class PriceChartingClient:
    def __init__(
        self,
        token_provider: Callable[[], str | None],
        limiter: SerialRateLimiter,
        *,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL,
        sleep: Callable[[float], "asyncio.Future | object"] = asyncio.sleep,
    ) -> None:
        self._token_provider = token_provider
        self.limiter = limiter
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )
        # Status for the operator dashboard.
        self.last_ok_at: float | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.auth_failed = False
        self.requests_total = 0
        self.rate_limited_total = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    def status(self) -> dict:
        configured = bool(self._token_provider())
        if not configured:
            state = "not_configured"
        elif self.auth_failed:
            state = "auth_failed"
        elif self.last_error_at and (not self.last_ok_at or self.last_error_at > self.last_ok_at):
            state = "error"
        elif self.last_ok_at:
            state = "connected"
        else:
            state = "idle"
        return {
            "state": state,
            "lastOkAt": self.last_ok_at,
            "lastError": self.last_error,
            "requestsTotal": self.requests_total,
            "rateLimitedTotal": self.rate_limited_total,
            "queueDepth": self.limiter.queue_depth,
            "minIntervalS": self.limiter.interval,
        }

    # -- public endpoints -----------------------------------------------------------------
    async def search(self, query: str) -> list[Candidate]:
        query = " ".join(query.split())[:200]
        if not query:
            return []
        data = await self._get("/api/products", {"q": query})
        products = data.get("products")
        if not isinstance(products, list):
            raise PriceChartingBadResponse("search response has no 'products' list")
        out: list[Candidate] = []
        seen: set[str] = set()
        for p in products[:20]:
            c = _parse_candidate(p)
            if c and c.product_id not in seen:
                seen.add(c.product_id)
                out.append(c)
        return out

    async def product(self, product_id: str) -> ProductPrices:
        pid = str(product_id).strip()
        if not pid.isdigit():
            raise PriceChartingBadResponse("product id must be numeric")
        data = await self._get("/api/product", {"id": pid}, not_found_ok=False)
        prices = _parse_product(data)
        if prices.product_id != pid:
            raise PriceChartingBadResponse(
                f"API returned product {prices.product_id} for requested id {pid}"
            )
        return prices

    async def product_by_upc(self, upc: str) -> ProductPrices | None:
        digits = "".join(ch for ch in upc if ch.isdigit())
        if not 8 <= len(digits) <= 14:
            raise ValueError("UPC must have 8-14 digits")
        try:
            data = await self._get("/api/product", {"upc": digits}, not_found_ok=False)
        except PriceChartingNotFound:
            return None
        return _parse_product(data)

    # -- transport ---------------------------------------------------------------------------
    async def _get(self, path: str, params: dict, not_found_ok: bool = False) -> dict:
        token = self._token_provider()
        if not token:
            raise PriceChartingAuthError("PriceCharting API token is not configured")
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self.limiter.slot():
                    self.requests_total += 1
                    resp = await self._http.get(path, params={"t": token, **params})
            except (httpx.TimeoutException, httpx.TransportError) as e:
                err: PriceChartingError = PriceChartingUnavailable(
                    f"network error contacting PriceCharting ({type(e).__name__})"
                )
                if attempt <= self.max_retries:
                    await self._backoff(attempt)
                    continue
                self._record_error(err)
                raise err from None

            try:
                return self._handle(resp, path)
            except PriceChartingRateLimited as e:
                self.rate_limited_total += 1
                retry_after = _retry_after(resp)
                delay = retry_after if retry_after is not None else 2.0 ** attempt
                self.limiter.penalize(min(60.0, delay))
                log.warning("PriceCharting rate limited (HTTP %s); backing off %.1fs", resp.status_code, delay)
                if attempt <= self.max_retries:
                    continue
                self._record_error(e)
                raise
            except PriceChartingUnavailable as e:
                if attempt <= self.max_retries:
                    await self._backoff(attempt)
                    continue
                self._record_error(e)
                raise
            except PriceChartingAuthError as e:
                self.auth_failed = True
                self._record_error(e)
                raise
            except PriceChartingNotFound:
                # A definitive answer from a healthy API.
                self.last_ok_at = time.time()
                raise
            except PriceChartingError as e:
                self._record_error(e)
                raise

    def _handle(self, resp: httpx.Response, path: str) -> dict:
        code = resp.status_code
        log.debug("PriceCharting %s -> HTTP %s", path, code)
        if code == 429:
            raise PriceChartingRateLimited("PriceCharting rate limit reached")
        if code >= 500:
            raise PriceChartingUnavailable(f"PriceCharting server error (HTTP {code})")
        try:
            data = resp.json()
        except ValueError:
            if code in (401, 403):
                raise PriceChartingAuthError("PriceCharting rejected the API token") from None
            if code == 200:
                raise PriceChartingUnavailable("PriceCharting returned a non-JSON response") from None
            raise PriceChartingBadResponse(f"unexpected HTTP {code} from PriceCharting") from None
        if not isinstance(data, dict):
            raise PriceChartingBadResponse("PriceCharting response is not a JSON object")
        status = data.get("status")
        msg = str(data.get("error-message") or "")[:300]
        if code in (401, 403):
            raise PriceChartingAuthError(f"PriceCharting rejected the API token: {msg or code}")
        if status == "success" and code == 200:
            self.auth_failed = False
            self.last_ok_at = time.time()
            return data
        lowered = msg.lower()
        if any(h in lowered for h in _AUTH_HINTS):
            raise PriceChartingAuthError(f"PriceCharting authentication error: {msg}")
        if "limit" in lowered or "too many" in lowered:
            raise PriceChartingRateLimited(f"PriceCharting rate limit: {msg}")
        if code == 404 or any(h in lowered for h in _NOT_FOUND_HINTS):
            raise PriceChartingNotFound(msg or "product not found")
        raise PriceChartingBadResponse(f"PriceCharting error (HTTP {code}): {msg or 'unknown'}")

    async def _backoff(self, attempt: int) -> None:
        delay = min(16.0, 2.0 ** (attempt - 1)) + random.uniform(0, 0.25)
        await self._sleep(delay)

    def _record_error(self, e: Exception) -> None:
        self.last_error = str(e)
        self.last_error_at = time.time()
        log.warning("PriceCharting request failed: %s", e)


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


def _clean_str(v: object, limit: int = 300) -> str | None:
    if isinstance(v, (int,)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return s[:limit] if s else None


def _parse_candidate(p: object) -> Candidate | None:
    if not isinstance(p, dict):
        return None
    pid = _clean_str(p.get("id"), 20)
    name = _clean_str(p.get("product-name"))
    console = _clean_str(p.get("console-name"), 80)
    if not pid or not pid.isdigit() or not name or not console:
        return None
    return Candidate(product_id=pid, product_name=name, console_name=console)


def _parse_product(data: dict) -> ProductPrices:
    pid = _clean_str(data.get("id"), 20)
    name = _clean_str(data.get("product-name"))
    console = _clean_str(data.get("console-name"), 80)
    if not pid or not pid.isdigit():
        raise PriceChartingBadResponse("product response is missing a valid 'id'")
    if not name or not console:
        raise PriceChartingBadResponse("product response is missing name/console")
    upc = _clean_str(data.get("upc"), 32)
    release = _clean_str(data.get("release-date"), 16)
    return ProductPrices(
        product_id=pid,
        product_name=name,
        console_name=console,
        loose_cents=parse_cents(data.get("loose-price")),
        cib_cents=parse_cents(data.get("cib-price")),
        new_cents=parse_cents(data.get("new-price")),
        retrieved_at=time.time(),
        upc=upc,
        release_date=release,
    )
