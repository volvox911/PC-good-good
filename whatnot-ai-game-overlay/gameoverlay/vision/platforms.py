"""Platform (console) normalization against PriceCharting's official console names.

Every PriceCharting ``console-name`` maps to a ``(region, base)`` pair, where
``base`` is the North-American name used as a family key. For example::

    "Nintendo 64"          -> ("NTSC-U", "Nintendo 64")
    "PAL Nintendo 64"      -> ("PAL",    "Nintendo 64")
    "Super Famicom"        -> ("NTSC-J", "Super Nintendo")
    "PAL Sega Mega Drive"  -> ("PAL",    "Sega Genesis")

Candidates are only accepted automatically when their family *and* region match the
recognized platform and region — so the Genesis and SNES releases of the same title,
or NTSC and PAL copies, can never be confused.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources

REGIONS = ("NTSC-U", "PAL", "NTSC-J", "Asia")

# Regional names whose base is not just the prefix-stripped name.
_SPECIAL_FAMILY: dict[str, tuple[str, str]] = {
    "Famicom": ("NTSC-J", "NES"),
    "Famicom Disk System": ("NTSC-J", "Famicom Disk System"),
    "Super Famicom": ("NTSC-J", "Super Nintendo"),
    "PAL Sega Mega Drive": ("PAL", "Sega Genesis"),
    "JP Sega Mega Drive": ("NTSC-J", "Sega Genesis"),
    "PAL Sega Mega CD": ("PAL", "Sega CD"),
    "JP Sega Mega CD": ("NTSC-J", "Sega CD"),
    "PAL Mega Drive 32X": ("PAL", "Sega 32X"),
    "JP Super 32X": ("NTSC-J", "Sega 32X"),
    "JP Sega Mark III": ("NTSC-J", "Sega Master System"),
    "JP PC Engine": ("NTSC-J", "TurboGrafx-16"),
    "JP PC Engine CD": ("NTSC-J", "TurboGrafx CD"),
    "PAL Sega Dreamcast": ("PAL", "Sega Dreamcast"),
    "JP Nintendo Switch": ("NTSC-J", "Nintendo Switch"),
    "JP Nintendo Switch 2": ("NTSC-J", "Nintendo Switch 2"),
    "Asian English Switch": ("Asia", "Nintendo Switch"),
    "Asian English Switch 2": ("Asia", "Nintendo Switch 2"),
    "Asian Xbox 360": ("Asia", "Xbox 360"),
    "JP Neo Geo Pocket": ("NTSC-J", "Neo Geo Pocket Color"),
    "PAL Neo Geo Pocket": ("PAL", "Neo Geo Pocket Color"),
    "PAL 3DO": ("PAL", "3DO"),
    "PAL Videopac G7000": ("PAL", "Magnavox Odyssey 2"),
    "PAL Videopac G7400": ("PAL", "Magnavox Odyssey 2"),
}

# Free-text aliases -> base (NTSC-U) console name.
_ALIASES: dict[str, str] = {
    "nes": "NES",
    "nintendo nes": "NES",
    "nintendo entertainment system": "NES",
    "famicom": "NES",
    "snes": "Super Nintendo",
    "super nes": "Super Nintendo",
    "super nintendo entertainment system": "Super Nintendo",
    "super famicom": "Super Nintendo",
    "n64": "Nintendo 64",
    "gamecube": "Gamecube",
    "nintendo gamecube": "Gamecube",
    "gcn": "Gamecube",
    "ngc": "Gamecube",
    "nintendo wii": "Wii",
    "nintendo wii u": "Wii U",
    "switch": "Nintendo Switch",
    "switch 2": "Nintendo Switch 2",
    "game boy": "GameBoy",
    "nintendo game boy": "GameBoy",
    "gb": "GameBoy",
    "game boy color": "GameBoy Color",
    "gbc": "GameBoy Color",
    "game boy advance": "GameBoy Advance",
    "gba": "GameBoy Advance",
    "ds": "Nintendo DS",
    "nds": "Nintendo DS",
    "3ds": "Nintendo 3DS",
    "genesis": "Sega Genesis",
    "mega drive": "Sega Genesis",
    "sega mega drive": "Sega Genesis",
    "megadrive": "Sega Genesis",
    "mega cd": "Sega CD",
    "sega mega cd": "Sega CD",
    "32x": "Sega 32X",
    "saturn": "Sega Saturn",
    "dreamcast": "Sega Dreamcast",
    "master system": "Sega Master System",
    "sms": "Sega Master System",
    "game gear": "Sega Game Gear",
    "playstation 1": "Playstation",
    "ps1": "Playstation",
    "psx": "Playstation",
    "ps one": "Playstation",
    "psone": "Playstation",
    "sony playstation": "Playstation",
    "ps2": "Playstation 2",
    "sony playstation 2": "Playstation 2",
    "ps3": "Playstation 3",
    "ps4": "Playstation 4",
    "ps5": "Playstation 5",
    "ps vita": "Playstation Vita",
    "vita": "Playstation Vita",
    "original xbox": "Xbox",
    "microsoft xbox": "Xbox",
    "xbox series x s": "Xbox Series X",
    "xbox series s": "Xbox Series X",
    "series x": "Xbox Series X",
    "atari vcs": "Atari 2600",
    "vcs": "Atari 2600",
    "lynx": "Atari Lynx",
    "atari jaguar": "Jaguar",
    "turbografx": "TurboGrafx-16",
    "turbografx 16": "TurboGrafx-16",
    "tg16": "TurboGrafx-16",
    "tg 16": "TurboGrafx-16",
    "pc engine": "TurboGrafx-16",
    "neo geo": "Neo Geo AES",
    "neogeo": "Neo Geo AES",
    "cdi": "CD-i",
    "philips cd i": "CD-i",
    "coleco vision": "Colecovision",
    "odyssey 2": "Magnavox Odyssey 2",
}


def _key(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


@lru_cache(maxsize=1)
def official_console_names() -> tuple[str, ...]:
    text = resources.files("gameoverlay.fixtures").joinpath("pricecharting_consoles.json").read_text("utf-8")
    return tuple(name for name, _ in json.loads(text))


@lru_cache(maxsize=1)
def _official_by_key() -> dict[str, str]:
    return {_key(n): n for n in official_console_names()}


def family(console_name: str) -> tuple[str, str]:
    """``(region, base)`` for a PriceCharting console name (unknown names pass through)."""
    name = " ".join(console_name.split())
    if name in _SPECIAL_FAMILY:
        return _SPECIAL_FAMILY[name]
    for prefix, region in (("PAL ", "PAL"), ("JP ", "NTSC-J"), ("Asian English ", "Asia"), ("Asian ", "Asia")):
        if name.startswith(prefix):
            base = name[len(prefix):]
            return region, canonical_base(base) or base
    return "NTSC-U", canonical_base(name) or name


def canonical_base(text: str | None) -> str | None:
    """Map free text (e.g. ``"N64"``, ``"Super NES"``) to an NTSC-U base console name."""
    if not text:
        return None
    k = _key(text)
    if not k:
        return None
    if k in _ALIASES:
        return _ALIASES[k]
    official = _official_by_key().get(k)
    if official:
        fam = _SPECIAL_FAMILY.get(official)
        if fam:
            return fam[1]
        for prefix in ("pal ", "jp ", "asian english ", "asian "):
            if k.startswith(prefix):
                return canonical_base(k[len(prefix):])
        return official
    # Strip common manufacturer prefixes ("Nintendo GameCube", "Sony PS2", "Sega Genesis").
    for pre in ("nintendo ", "sony ", "sega ", "microsoft ", "atari ", "nec "):
        if k.startswith(pre):
            r = canonical_base(k[len(pre):])
            if r:
                return r
            sega = _official_by_key().get(k)
            if sega:
                return sega
    return None


def base_console_names() -> list[str]:
    """NTSC-U family names offered to the vision model as the platform vocabulary."""
    seen: list[str] = []
    skip_words = ("magazine", "power", "informer", "gamepro", "players", "monthly", "gamer", "pc games",
                  "strategy guide", "wholesale", "figures", "cards", "amiibo", "skylanders", "starlink",
                  "disney infinity", "lego dimensions", "funkeys", "retro gamer")
    for n in official_console_names():
        if any(w in n.lower() for w in skip_words):
            continue
        region, base = family(n)
        if region == "NTSC-U" and base not in seen:
            seen.append(base)
    return seen


def console_name_for(base: str, region: str) -> str | None:
    """Official PriceCharting console name for a (base, region) pair, if one exists."""
    for n in official_console_names():
        if family(n) == (region, base):
            return n
    return None
