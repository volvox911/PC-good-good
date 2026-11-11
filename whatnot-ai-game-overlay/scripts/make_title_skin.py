"""Builds header-skin-title.png — the price strip's frame without the three price plates.

Used when the operator hides market prices on the stream ("title only"). It is derived
entirely from the operator's own frame art (header-skin.png, which is never modified): the
plain rusted metal behind the title is copied, mirrored, over the divider and the plates,
with feathered edges; the slash, rails and rivets are untouched.

    python scripts/make_title_skin.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

OVERLAY = Path(__file__).resolve().parents[1] / "gameoverlay" / "web" / "overlay"

# Pixel geometry of the 941x150 frame art.
SRC_X0, SRC_X1 = 175, 425  # plain metal behind the title
DST_X0, DST_X1 = 420, 917  # divider + plates
ROW0, ROW1 = 21, 130  # interior between the top and bottom rails
RIVET = (919, 115, 11)  # right-hand rivet (x, y, radius) — keep


def build(skin: Image.Image) -> Image.Image:
    a = np.asarray(skin.convert("RGBA")).astype(np.float32)
    h, w = a.shape[:2]
    tile = a[:, SRC_X0:SRC_X1, :3]
    strip = np.concatenate([tile, tile[:, ::-1]], axis=1)  # mirrored, so the join is seamless
    while strip.shape[1] < DST_X1 - DST_X0:
        strip = np.concatenate([strip, strip[:, ::-1]], axis=1)
    fill = a[..., :3].copy()
    fill[:, DST_X0:DST_X1] = strip[:, : DST_X1 - DST_X0]

    mask = np.zeros((h, w), np.float32)
    mask[ROW0:ROW1, DST_X0:DST_X1] = 1.0
    yy, xx = np.mgrid[0:h, 0:w]
    rx, ry, rr = RIVET
    mask[(xx - rx) ** 2 + (yy - ry) ** 2 <= rr * rr] = 0.0
    soft = np.asarray(Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.6)), np.float32) / 255.0
    # a longer cross-fade where the copied metal meets the original on the left
    ramp = np.clip((xx - DST_X0) / 14.0, 0.0, 1.0)
    alpha = (soft * ramp)[..., None]

    out = a.copy()
    out[..., :3] = a[..., :3] * (1 - alpha) + fill * alpha
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")


def main() -> None:
    out = build(Image.open(OVERLAY / "header-skin.png"))
    out.save(OVERLAY / "header-skin-title.png", optimize=True)
    print("wrote", OVERLAY / "header-skin-title.png", out.size)


if __name__ == "__main__":
    main()
