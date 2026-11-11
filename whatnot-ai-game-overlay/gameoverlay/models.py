"""Core domain models shared by the pipeline, API and overlay hub."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from .money import cents_to_usd_float, format_usd

Freshness = Literal["fresh", "cached", "stale"]
RecordSource = Literal["auto", "manual", "recall", "demo"]


@dataclass(frozen=True)
class Candidate:
    """A PriceCharting search result (``/api/products``)."""

    product_id: str
    product_name: str
    console_name: str

    def to_dict(self) -> dict:
        return {
            "productId": self.product_id,
            "productName": self.product_name,
            "consoleName": self.console_name,
        }


@dataclass(frozen=True)
class ProductPrices:
    """Prices for exactly one PriceCharting product (``/api/product``)."""

    product_id: str
    product_name: str
    console_name: str
    loose_cents: int | None
    cib_cents: int | None
    new_cents: int | None
    retrieved_at: float
    upc: str | None = None
    release_date: str | None = None

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (now or time.time()) - self.retrieved_at)

    def to_dict(self) -> dict:
        return {
            "productId": self.product_id,
            "productName": self.product_name,
            "consoleName": self.console_name,
            "looseCents": self.loose_cents,
            "cibCents": self.cib_cents,
            "newCents": self.new_cents,
            "loose": format_usd(self.loose_cents),
            "complete": format_usd(self.cib_cents),
            "new": format_usd(self.new_cents),
            "retrievedAt": iso(self.retrieved_at),
            "upc": self.upc,
            "releaseDate": self.release_date,
            "productUrl": product_url(self.product_id),
        }


@dataclass(frozen=True)
class PriceLookup:
    prices: ProductPrices
    freshness: Freshness
    refreshed: bool  # True if this lookup hit the API


@dataclass(frozen=True)
class VerifiedRecord:
    """The one atomic record the viewer overlay may display.

    Title, platform and all three prices always come from the *same* PriceCharting
    product (``product_id``); there is no way to update one without the others.
    """

    title: str
    platform: str
    product_id: str
    loose_cents: int | None
    cib_cents: int | None
    new_cents: int | None
    retrieved_at: float
    freshness: Freshness
    source: RecordSource
    demo: bool = False
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_prices(
        cls,
        prices: ProductPrices,
        freshness: Freshness,
        source: RecordSource,
        demo: bool = False,
    ) -> "VerifiedRecord":
        return cls(
            title=prices.product_name,
            platform=display_platform(prices.console_name),
            product_id=prices.product_id,
            loose_cents=prices.loose_cents,
            cib_cents=prices.cib_cents,
            new_cents=prices.new_cents,
            retrieved_at=prices.retrieved_at,
            freshness=freshness,
            source=source,
            demo=demo,
        )

    def to_overlay_dict(self, include_prices: bool = True) -> dict:
        """Public payload — the only game data that reaches OBS clients.

        With ``include_prices=False`` (operator chose "title only") the market values are
        left out entirely, so they never reach the stream page.
        """
        d = {
            "title": self.title,
            "platform": self.platform,
            "productId": self.product_id,
            "verified": True,
            "looseUsd": cents_to_usd_float(self.loose_cents),
            "completeUsd": cents_to_usd_float(self.cib_cents),
            "newUsd": cents_to_usd_float(self.new_cents),
            "looseCents": self.loose_cents,
            "completeCents": self.cib_cents,
            "newCents": self.new_cents,
            "retrievedAt": iso(self.retrieved_at),
            "freshness": self.freshness,
            "demo": self.demo,
        }
        if not include_prices:
            for kind in ("loose", "complete", "new"):
                d[kind + "Usd"] = None
                d[kind + "Cents"] = None
            d["freshness"] = "fresh"  # nothing price-related is shown, so nothing to label
            d["pricesHidden"] = True
        return d


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def product_url(product_id: str) -> str:
    return f"https://www.pricecharting.com/game/{product_id}"


def display_platform(console_name: str) -> str:
    """Viewer-facing console label, e.g. ``Nintendo 64`` -> ``NINTENDO 64``.

    ``Playstation`` is shown as ``PLAYSTATION``; region prefixes are kept
    (``PAL Nintendo 64`` -> ``PAL NINTENDO 64``).
    """
    name = " ".join(console_name.split())
    special = {
        "NES": "NINTENDO NES",
        "Playstation": "PLAYSTATION",
        "Xbox": "ORIGINAL XBOX",
        "Gamecube": "GAMECUBE",
        "GameBoy": "GAME BOY",
        "GameBoy Color": "GAME BOY COLOR",
        "GameBoy Advance": "GAME BOY ADVANCE",
    }
    return special.get(name, name).upper()
