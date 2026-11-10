"""Configuration.

Two layers:

* :class:`AppConfig` — process/bootstrap settings read from environment variables
  (and an optional ``.env`` file). Covers environment (development/test/production),
  bind address, data directory and demo mode. Secrets are *not* stored here; see
  :mod:`gameoverlay.secrets_store`.
* :class:`RuntimeSettings` — operator-adjustable settings persisted in SQLite and
  edited from the private dashboard (camera source, thresholds, overlay layout...).
"""

from __future__ import annotations

import ipaddress
import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


def default_data_dir() -> Path:
    """Per-user data directory, kept outside the source tree (never committed)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "WhatnotGameOverlay"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "whatnot-game-overlay"


class AppConfig(BaseSettings):
    """Bootstrap configuration from the environment. Prefix: ``WGO_``."""

    model_config = SettingsConfigDict(
        env_prefix="WGO_",
        env_file=(str(PROJECT_ROOT / ".env"),),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.PRODUCTION
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    data_dir: Path = Field(default_factory=default_data_dir)
    demo: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    # Binding to anything other than loopback must be an explicit, deliberate choice.
    allow_non_loopback: bool = False
    # Disable keyring (e.g. CI). Secrets then come from env vars or the data-dir file.
    disable_keyring: bool = False

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(os.path.expandvars(os.path.expanduser(v)))
        return v

    @model_validator(mode="after")
    def _check_bind(self) -> "AppConfig":
        if not is_loopback_host(self.host) and not self.allow_non_loopback:
            raise ValueError(
                f"Refusing to bind to non-loopback address {self.host!r}. The dashboard must "
                "not be exposed to the network by default. Set WGO_ALLOW_NON_LOOPBACK=true "
                "only if you understand the risk."
            )
        return self

    @property
    def db_path(self) -> Path:
        name = "test.sqlite3" if self.env == Environment.TEST else "overlay.sqlite3"
        return self.data_dir / name

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def base_url(self) -> str:
        host = self.host if self.host not in ("0.0.0.0", "::") else "127.0.0.1"
        return f"http://{host}:{self.port}"


def is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------------------
# Runtime (operator) settings
# --------------------------------------------------------------------------------------

SizePreset = Literal["compact", "balanced", "large"]
PositionPreset = Literal[
    "bottom-left",
    "bottom-center",
    "bottom-right",
    "top-left",
    "top-center",
    "top-right",
    "side-left",
    "side-right",
]
OrientationMode = Literal["auto", "portrait", "landscape"]
StalePolicy = Literal["hide", "show_labeled"]
Region = Literal["NTSC-U", "PAL", "NTSC-J"]


class CropRegion(BaseModel):
    """Normalized (0..1) rectangle of the presentation area inside the camera frame."""

    x: float = Field(default=0.0, ge=0.0, le=1.0)
    y: float = Field(default=0.0, ge=0.0, le=1.0)
    w: float = Field(default=1.0, gt=0.0, le=1.0)
    h: float = Field(default=1.0, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside(self) -> "CropRegion":
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("crop region must lie inside the frame")
        if self.w < 0.05 or self.h < 0.05:
            raise ValueError("crop region is too small (min 5% of frame)")
        return self


class OBSSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=4455, ge=1, le=65535)
    camera_source: str | None = None
    rotation: Literal[0, 90, 180, 270] = 0
    crop: CropRegion = Field(default_factory=CropRegion)
    # Long side of the high-quality still (px); 0 = source native resolution.
    still_max_width: int = Field(default=1920, ge=0, le=4096)
    still_jpeg_quality: int = Field(default=92, ge=50, le=100)
    motion_sample_width: int = Field(default=320, ge=64, le=1024)
    request_timeout_s: float = Field(default=6.0, ge=1.0, le=30.0)


class DetectionSettings(BaseModel):
    sample_interval_ms: int = Field(default=400, ge=100, le=5000)
    # Mean absolute frame difference (0..1) above which we consider the scene "moving".
    motion_threshold: float = Field(default=0.035, ge=0.001, le=0.5)
    # Consecutive calm samples required before a frame is "stable".
    stable_frames_required: int = Field(default=3, ge=1, le=20)
    # How different a stable frame must be from the displayed game's frame to count
    # as a new item (0..1 localized perceptual distance; re-posed same game ~0.02-0.16,
    # a different game ~0.35+).
    change_threshold: float = Field(default=0.22, ge=0.02, le=0.9)
    # Distance under which a stable frame is considered the same as a remembered game
    # (visual memory reuse — deliberately strict).
    same_item_threshold: float = Field(default=0.12, ge=0.0, le=0.5)
    # Reuse earlier identification of a visually identical item within the session.
    visual_memory_enabled: bool = True
    min_sharpness: float = Field(default=60.0, ge=0.0, le=5000.0)
    min_brightness: float = Field(default=35.0, ge=0.0, le=255.0)
    max_brightness: float = Field(default=235.0, ge=0.0, le=255.0)
    burst_frames: int = Field(default=2, ge=1, le=5)
    burst_spacing_ms: int = Field(default=250, ge=50, le=2000)


class RecognitionSettings(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    request_timeout_s: float = Field(default=45.0, ge=5.0, le=300.0)
    max_image_long_side: int = Field(default=1568, ge=512, le=2576)
    # Minimum combined (title-similarity) score to auto-accept a PriceCharting product.
    accept_score: float = Field(default=88.0, ge=50.0, le=100.0)
    # Minimum lead over the runner-up candidate on the same platform.
    accept_margin: float = Field(default=6.0, ge=0.0, le=50.0)
    min_confidence: Literal["low", "medium", "high"] = "medium"
    require_corroboration: bool = True
    default_region: Region = "NTSC-U"
    session_spend_limit_usd: float = Field(default=5.00, ge=0.0, le=1000.0)
    max_requests_per_minute: int = Field(default=12, ge=1, le=120)
    use_server_fallbacks: bool = True


class PricingSettings(BaseModel):
    # PriceCharting allows at most 1 call per second. Never below 1.05 (safety margin).
    min_request_interval_s: float = Field(default=1.2, ge=1.05, le=60.0)
    request_timeout_s: float = Field(default=10.0, ge=2.0, le=60.0)
    max_retries: int = Field(default=3, ge=0, le=6)
    price_refresh_s: int = Field(default=3600, ge=60, le=7 * 24 * 3600)
    stale_policy: StalePolicy = "hide"
    max_stale_s: int = Field(default=24 * 3600, ge=60, le=30 * 24 * 3600)
    search_cache_s: int = Field(default=7 * 24 * 3600, ge=0, le=90 * 24 * 3600)


class DisplaySettings(BaseModel):
    # Hide the old game's prices when a different, unidentified item is presented.
    unidentified_grace_s: float = Field(default=6.0, ge=0.0, le=120.0)
    # Hide when the presentation area is empty for this long (0 = never auto-hide).
    empty_grace_s: float = Field(default=10.0, ge=0.0, le=600.0)
    # Auto-hide after this long on screen (0 = no timeout).
    display_timeout_s: float = Field(default=0.0, ge=0.0, le=3600.0)
    # Minimum time a game stays on screen before an automatic replacement.
    min_display_s: float = Field(default=2.5, ge=0.0, le=30.0)
    # Overlay hides the record if it loses the backend connection this long.
    overlay_disconnect_hide_s: float = Field(default=120.0, ge=5.0, le=3600.0)


class OverlaySettings(BaseModel):
    size: SizePreset = "balanced"
    position: PositionPreset = "top-center"
    orientation: OrientationMode = "auto"
    opacity: float = Field(default=1.0, ge=0.3, le=1.0)
    font_scale: float = Field(default=1.0, ge=0.8, le=1.3)
    # Extra distance from the anchored edge, as a fraction of the canvas height.
    edge_offset: float = Field(default=0.008, ge=0.0, le=0.3)
    # Source attribution line is off by default; enable it if your PriceCharting
    # commercial agreement requires on-screen attribution.
    show_attribution: bool = False
    attribution_text: str = Field(default="SOURCE: PRICECHARTING", max_length=60)
    show_retrieval_time: bool = False
    # Keep the band on screen between games (cleared, no prices) instead of hiding it.
    idle_panel: bool = True
    idle_title: str = Field(default="Price Guide", max_length=40)
    show_product_link: bool = False
    reduced_motion: bool = False
    canvas_width: int = Field(default=1080, ge=320, le=7680)
    canvas_height: int = Field(default=1920, ge=320, le=7680)

    @field_validator("attribution_text", "idle_title")
    @classmethod
    def _printable(cls, v: str) -> str:
        v = " ".join(v.split())
        if any(ord(c) < 32 for c in v):
            raise ValueError("control characters are not allowed")
        return v


SETTINGS_VERSION = 2


def migrate_settings(raw: dict) -> dict:
    """Upgrade settings saved by older versions (stored as JSON in the database)."""
    raw = dict(raw)
    version = int(raw.get("settings_version") or 1)
    if version < 2:
        # v2: the "AS OF" time is no longer shown by default (operator request).
        overlay = dict(raw.get("overlay") or {})
        overlay["show_retrieval_time"] = False
        # v2: the sign now sits flush at the top; move it up unless the operator changed it.
        if overlay.get("edge_offset") in (None, 0.035):
            overlay["edge_offset"] = 0.008
        raw["overlay"] = overlay
    raw["settings_version"] = SETTINGS_VERSION
    return raw


class RuntimeSettings(BaseModel):
    obs: OBSSettings = Field(default_factory=OBSSettings)
    detection: DetectionSettings = Field(default_factory=DetectionSettings)
    recognition: RecognitionSettings = Field(default_factory=RecognitionSettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    overlay: OverlaySettings = Field(default_factory=OverlaySettings)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    setup_completed: bool = False
    settings_version: int = SETTINGS_VERSION

    def merged(self, patch: dict) -> "RuntimeSettings":
        """Return a validated copy with ``patch`` deep-merged in."""
        base = self.model_dump()
        _deep_merge(base, patch)
        return RuntimeSettings.model_validate(base)


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
