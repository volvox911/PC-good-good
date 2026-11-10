"""Synthetic camera frames for demo mode and tests (no real box art is used)."""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont


@dataclass(frozen=True)
class DemoItem:
    product_id: str | None  # expected PriceCharting product (None = not a game / unknown)
    label_title: str
    label_platform: str
    platform: str | None  # canonical base platform the mock "Claude" reports
    region: str = "NTSC-U"
    edition: str | None = None
    item_type: str = "video_game"
    confidence: str = "high"
    color: tuple[int, int, int] = (40, 60, 160)


DEMO_ITEMS: list[DemoItem] = [
    DemoItem("900101", "THE LEGEND OF ZELDA: OCARINA OF TIME", "NINTENDO 64", "Nintendo 64", color=(150, 120, 40)),
    DemoItem("900302", "MORTAL KOMBAT", "SUPER NINTENDO ENTERTAINMENT SYSTEM", "Super Nintendo", color=(90, 90, 90)),
    DemoItem("900601", "STADIUM EVENTS", "NINTENDO ENTERTAINMENT SYSTEM", "NES", color=(160, 30, 30)),
    DemoItem("900401", "RESIDENT EVIL", "PlayStation", "Playstation", color=(30, 30, 30)),
    DemoItem("900501", "EARTHBOUND", "SUPER NINTENDO", "Super Nintendo", color=(60, 140, 90)),
    DemoItem("900801", "TEENAGE MUTANT NINJA TURTLES II: THE ARCADE GAME", "NINTENDO ENTERTAINMENT SYSTEM", "NES", color=(40, 120, 50)),
    DemoItem("900701", "MADDEN NFL 2004", "PlayStation 2", "Playstation 2", color=(20, 50, 120)),
    DemoItem("900102", "THE LEGEND OF ZELDA: OCARINA OF TIME", "NINTENDO 64  PLAYER'S CHOICE", "Nintendo 64", edition="Player's Choice", color=(200, 170, 30)),
    DemoItem("900301", "MORTAL KOMBAT", "SEGA GENESIS", "Sega Genesis", color=(15, 15, 15)),
    DemoItem("900904", "CHRONO TRIGGER", "SUPER NINTENDO", "Super Nintendo", color=(120, 60, 140)),
]


@lru_cache(maxsize=8)
def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    names = ["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"] if bold else [
        "DejaVuSans.ttf", "arial.ttf", "LiberationSans-Regular.ttf"
    ]
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_box(item: DemoItem, size: tuple[int, int] = (520, 700)) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, item.color)
    d = ImageDraw.Draw(img)
    # platform banner
    d.rectangle([0, 0, w, 90], fill=(235, 235, 235))
    pf = _font(30)
    for i, line in enumerate(_wrap(d, item.label_platform, pf, w - 30)[:2]):
        d.text((15, 10 + i * 36), line, fill=(20, 20, 20), font=pf)
    # art blocks
    rnd = random.Random(item.label_title)
    for _ in range(14):
        x, y = rnd.randint(0, w), rnd.randint(110, h - 160)
        r = rnd.randint(20, 90)
        col = tuple(min(255, max(0, c + rnd.randint(-70, 70))) for c in item.color)
        d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    # title plate
    tf = _font(44)
    lines = _wrap(d, item.label_title, tf, w - 40)[:4]
    th = 54 * len(lines) + 30
    d.rectangle([0, h - th - 20, w, h - 20], fill=(250, 245, 225))
    for i, line in enumerate(lines):
        d.text((20, h - th - 5 + i * 54), line, fill=(10, 10, 10), font=tf)
    if item.edition:
        d.rectangle([0, 92, w, 140], fill=(250, 210, 0))
        d.text((15, 98), item.edition.upper(), fill=(0, 0, 0), font=_font(32))
    return img


def render_scene(
    item: DemoItem | None,
    t: float = 0.0,
    size: tuple[int, int] = (1280, 720),
    jitter: float = 0.0,
    blur: float = 0.0,
) -> Image.Image:
    w, h = size
    bg = Image.new("RGB", size, (72, 52, 38))
    d = ImageDraw.Draw(bg)
    for i in range(0, w, 64):  # wood grain
        d.line([(i, 0), (i + 30, h)], fill=(80, 58, 42), width=18)
    if item is not None:
        box = render_box(item).resize((int(h * 0.62 * 0.74), int(h * 0.62)))
        dx = int(math.sin(t * 7.0) * jitter * 40)
        dy = int(math.cos(t * 5.0) * jitter * 25)
        bx = (w - box.width) // 2 + dx
        by = (h - box.height) // 2 + dy
        bg.paste(box, (bx, by))
        # a "hand" holding the box
        d.ellipse([bx - 40, by + box.height - 120, bx + 70, by + box.height + 10], fill=(214, 170, 140))
    if blur:
        bg = bg.filter(ImageFilter.GaussianBlur(blur))
    return bg


def to_jpeg(img: Image.Image, width: int | None = None, quality: int = 90) -> bytes:
    if width and width < img.width:
        img = img.resize((width, max(1, int(img.height * width / img.width))))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(30, min(95, quality)))
    return buf.getvalue()
