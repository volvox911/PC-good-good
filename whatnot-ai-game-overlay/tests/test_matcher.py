"""Requirements 2-5: ambiguous titles, alternate consoles, editions/regions, low confidence."""

from __future__ import annotations

import pytest

from gameoverlay.config import RecognitionSettings
from gameoverlay.demo.mock_vision import identification_for
from gameoverlay.demo.scene import DemoItem
from gameoverlay.models import Candidate
from gameoverlay.pipeline.matcher import (
    Matcher,
    norm_text,
    split_edition,
    title_score,
    valid_upc,
)


def rec(title, platform, region="NTSC-U", edition=None, confidence="high", **kw):
    item = DemoItem(None, title, platform or "", platform, region=region, edition=edition, confidence=confidence)
    r = identification_for(item, title=title, **kw)
    return r


@pytest.fixture
def matcher(pricing, db):
    cfg = RecognitionSettings()
    m = Matcher(pricing, db, lambda: cfg)
    m.cfg = cfg
    return m


def test_title_normalization():
    assert norm_text("The Legend of Zelda: Ocarina of Time") == "zelda ocarina of time"
    assert norm_text("Mortal Kombat II") == "mortal kombat 2"
    assert norm_text("Pokémon Snap") == "pokemon snap"
    assert split_edition("Zelda Ocarina of Time [Player's Choice]") == ("Zelda Ocarina of Time", "Player's Choice")


def test_sequel_numbers_are_significant():
    assert title_score("Super Mario Bros.", "Super Mario Bros") == 100
    assert title_score("Super Mario Bros.", "Super Mario Bros 3") < 85
    assert title_score("Mortal Kombat", "Mortal Kombat II") < 85
    assert title_score("Zelda Ocarina of Time", "Zelda Ocarina of Time 3D") < 85


def test_upc_check_digit():
    assert valid_upc("045496830434")
    assert not valid_upc("045496830435")
    assert not valid_upc("123")
    assert not valid_upc(None)


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("Sega Genesis", "900301"),
        ("Super Nintendo", "900302"),
        ("GameBoy", "900303"),
        ("Sega CD", "900304"),
    ],
)
async def test_same_title_different_consoles(matcher, platform, expected):
    r = await matcher.resolve(rec("Mortal Kombat", platform))
    assert r.status == "matched" and r.product.product_id == expected


async def test_resident_evil_playstation_vs_gamecube(matcher):
    ps = await matcher.resolve(rec("Resident Evil", "Playstation"))
    gc = await matcher.resolve(rec("Resident Evil", "Gamecube"))
    assert ps.product.product_id == "900401"
    assert gc.product.product_id == "900404"


async def test_super_mario_bros_is_not_bros_3(matcher):
    r = await matcher.resolve(rec("Super Mario Bros.", "NES"))
    assert r.status == "matched" and r.product.product_id == "900201"


async def test_standard_edition_preferred_when_no_banner(matcher):
    r = await matcher.resolve(rec("The Legend of Zelda: Ocarina of Time", "Nintendo 64"))
    assert r.product.product_id == "900101"


async def test_players_choice_edition(matcher):
    r = await matcher.resolve(rec("The Legend of Zelda: Ocarina of Time", "Nintendo 64", edition="Player's Choice"))
    assert r.status == "matched" and r.product.product_id == "900102"


async def test_edition_in_title_text(matcher):
    r = await matcher.resolve(rec("Zelda Ocarina of Time Collector's Edition", "Nintendo 64"))
    assert r.product.product_id == "900103"


async def test_greatest_hits(matcher):
    r = await matcher.resolve(rec("Resident Evil", "Playstation", edition="Greatest Hits"))
    assert r.product.product_id == "900402"


async def test_pal_region_variant(matcher):
    r = await matcher.resolve(rec("The Legend of Zelda: Ocarina of Time", "Nintendo 64", region="PAL"))
    assert r.status == "matched" and r.product.product_id == "900104"
    assert r.product.console_name == "PAL Nintendo 64"


async def test_low_confidence_never_auto_matches(matcher):
    r = await matcher.resolve(rec("Mortal Kombat", "Sega Genesis", confidence="low"))
    assert r.status == "ambiguous" and r.product is None
    assert any(s.candidate.product_id == "900301" for s in r.suggestions)


async def test_obscured_title_medium_confidence_needs_confirmation(matcher):
    r = await matcher.resolve(rec("Mortal Kombat", "Sega Genesis", confidence="medium", title_obscured=True))
    assert r.status == "ambiguous"


async def test_unknown_platform_never_auto_matches(matcher):
    r = await matcher.resolve(rec("Mortal Kombat", None))
    assert r.status == "ambiguous"
    assert len({s.candidate.console_name for s in r.suggestions}) >= 3


async def test_platform_without_listing(matcher):
    r = await matcher.resolve(rec("Mortal Kombat", "Atari 2600"))
    assert r.status == "no_match" and r.product is None


async def test_ambiguous_close_candidates(matcher, mock_pc):
    # Two near-identical products on the same console -> must ask the operator.
    mock_pc.catalog["909001"] = {"status": "success", "id": "909001", "product-name": "Sonic the Hedgehog 2 ", "console-name": "Sega Genesis"}
    mock_pc.catalog["909002"] = {"status": "success", "id": "909002", "product-name": "Sonic the Hedgehog 2!", "console-name": "Sega Genesis"}
    r = await matcher.resolve(rec("Sonic the Hedgehog 2", "Sega Genesis"))
    assert r.status == "ambiguous"


async def test_not_a_game(matcher):
    r = await matcher.resolve(identification_for(None))
    assert r.status == "not_a_game"
    r2 = await matcher.resolve(rec("SNES Controller", "Super Nintendo", item_type="accessory"))
    assert r2.status == "not_a_game"


async def test_match_cache_avoids_api_calls(matcher, mock_pc):
    r1 = await matcher.resolve(rec("Chrono Trigger", "Super Nintendo"))
    assert r1.status == "matched" and r1.via == "search"
    n = len(mock_pc.calls)
    r2 = await matcher.resolve(rec("Chrono Trigger", "SNES"))
    assert r2.via == "match_cache" and len(mock_pc.calls) == n


async def test_manual_correction_overrides_and_persists(matcher):
    r = rec("Mortal Kombat", "Sega Genesis", confidence="low")
    first = await matcher.resolve(r)
    assert first.status == "ambiguous" and first.key
    matcher.remember(first.key, Candidate("900301", "Mortal Kombat", "Sega Genesis"), "manual")
    again = await matcher.resolve(r)
    assert again.status == "matched" and again.via == "match_cache" and again.product.product_id == "900301"
    # an automatic match can never overwrite a manual correction
    matcher.remember(first.key, Candidate("900302", "Mortal Kombat", "Super Nintendo"), "auto")
    assert (await matcher.resolve(r)).product.product_id == "900301"


async def test_upc_with_mismatching_title_is_not_auto_accepted(matcher, mock_pc):
    """Review #7: a misread/foreign barcode must agree with the printed title."""
    mock_pc.catalog["900301"]["upc"] = "045496830434"  # valid check digit, Mortal Kombat Genesis
    r = await matcher.resolve(rec("Sonic the Hedgehog 2", "Sega Genesis", upc_digits="045496830434"))
    assert r.product is None or r.product.product_id != "900301"
    r2 = await matcher.resolve(rec("Mortal Kombat", "Sega Genesis", upc_digits="045496830434"))
    assert r2.status == "matched" and r2.via == "upc"
