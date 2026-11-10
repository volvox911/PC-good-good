"""Requirement 11: exact cents -> dollars conversion; requirement 10: missing -> N/A."""

from decimal import Decimal

import pytest

from gameoverlay.money import cents_to_decimal, cents_to_usd_float, format_usd, parse_cents


@pytest.mark.parametrize(
    "cents,expected",
    [
        (4295, "$42.95"),
        (4250, "$42.50"),
        (10999, "$109.99"),
        (59900, "$599.00"),
        (5, "$0.05"),
        (99, "$0.99"),
        (100, "$1.00"),
        (123456, "$1,234.56"),
        (12345678, "$123,456.78"),  # six figures
        (99999999, "$999,999.99"),
        (None, "N/A"),
    ],
)
def test_format_usd(cents, expected):
    assert format_usd(cents) == expected


def test_4295_is_never_shown_as_dollars():
    assert format_usd(4295) not in ("$4,295", "$4295", "$4,295.00")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (4295, 4295),
        ("4295", 4295),
        (4295.0, 4295),
        (None, None),
        (0, None),  # zero means "no data" -> N/A, never $0.00
        (-5, None),
        (True, None),
        (False, None),
        ("", None),
        ("42.95", None),  # API sends integer pennies; dollars-looking strings are rejected
        ("abc", None),
        (42.5, None),
        (float("nan"), None),
        (float("inf"), None),
        ([], None),
        ({}, None),
    ],
)
def test_parse_cents(raw, expected):
    assert parse_cents(raw) == expected


def test_decimal_exact():
    assert cents_to_decimal(4295) == Decimal("42.95")
    assert cents_to_usd_float(4295) == 42.95
    assert cents_to_usd_float(None) is None
