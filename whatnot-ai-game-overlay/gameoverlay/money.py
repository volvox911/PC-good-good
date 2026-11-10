"""Exact conversion of PriceCharting integer pennies to USD.

PriceCharting encodes prices as an integer number of pennies (``4295`` -> ``$42.95``).
Everything here uses integer arithmetic / :class:`~decimal.Decimal` — never floats —
so values are displayed at 100% of the API value with no rounding drift.

A missing, null, zero, negative or malformed price is treated as "no data" and is
displayed as ``N/A`` — never as ``$0.00``.
"""

from __future__ import annotations

from decimal import Decimal

NA = "N/A"


def parse_cents(value: object) -> int | None:
    """Validate a raw API price field. Returns pennies, or None for no/invalid data."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        cents = value
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or not value.is_integer():
            return None
        cents = int(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s.isdigit() or len(s) > 12:
            return None
        cents = int(s)
    else:
        return None
    if cents <= 0 or cents > 99_999_999_999:
        return None
    return cents


def cents_to_decimal(cents: int | None) -> Decimal | None:
    if cents is None:
        return None
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def cents_to_usd_float(cents: int | None) -> float | None:
    """For JSON consumers that expect a number; display code formats from cents."""
    d = cents_to_decimal(cents)
    return float(d) if d is not None else None


def format_usd(cents: int | None) -> str:
    """``4295`` -> ``"$42.95"``; ``12345678`` -> ``"$123,456.78"``; ``None`` -> ``"N/A"``."""
    if cents is None:
        return NA
    dollars, rem = divmod(int(cents), 100)
    return f"${dollars:,}.{rem:02d}"
