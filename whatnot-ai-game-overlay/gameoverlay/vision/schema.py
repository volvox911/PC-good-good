"""Structured output schema for Claude Vision game identification."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


class GameIdentification(BaseModel):
    """What Claude reports about the presented item. Never contains prices."""

    model_config = ConfigDict(extra="forbid")

    item_present: bool = Field(description="True if any physical item is being presented in the image.")
    item_type: Literal["video_game", "console_hardware", "accessory", "other", "none"] = Field(
        description="video_game for a game cartridge, disc, case or box."
    )
    title: str | None = Field(description="Exact game title as printed on the item, or null if unreadable.")
    alternate_titles: list[str] = Field(
        description="Other plausible official spellings of the same title (e.g. without subtitle). Max 3."
    )
    platform: str | None = Field(
        description="Console, chosen from the provided platform list (North American family name), or null."
    )
    platform_evidence: str = Field(description="Visible evidence for the platform (logos, label text, cartridge/case shape).")
    region: Literal["NTSC-U", "PAL", "NTSC-J", "unknown"] = Field(
        description="NTSC-U (North America), PAL (Europe/Australia), NTSC-J (Japan), or unknown."
    )
    region_evidence: str | None = Field(description="Visible evidence for the region, if any.")
    edition: str | None = Field(
        description="Variant/edition printed on the item (e.g. Player's Choice, Greatest Hits, Platinum, "
        "Collector's Edition, Classics), or null for a standard release."
    )
    edition_evidence: str | None = Field(description="Visible evidence for the edition, if any.")
    media_format: Literal["cartridge", "disc", "boxed_game", "case", "manual_only", "other", "unknown"]
    visible_text: list[str] = Field(description="Legible text lines on the item, most prominent first. Max 12.")
    upc_digits: str | None = Field(description="Barcode digits if a UPC/EAN is clearly legible, else null.")
    title_obscured: bool = Field(description="True if the title is partially hidden, cut off, blurred or glared.")
    confidence: Literal["high", "medium", "low"] = Field(
        description="Confidence in BOTH the title and the platform. Use low if either is a guess."
    )
    needs_more_images: bool = Field(description="True if another, clearer photo would substantially help.")
