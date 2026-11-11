"""Quality checks, crop/rotate, fingerprints and stability detection on fixture images."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from gameoverlay.config import CropRegion
from gameoverlay.demo.scene import DEMO_ITEMS, render_scene, to_jpeg
from gameoverlay.vision.change import ChangeDetector
from gameoverlay.vision.imaging import (
    crop,
    decode,
    fingerprint,
    fingerprint_distance,
    measure_quality,
    prepare,
    rotate,
)


def img(item=DEMO_ITEMS[0], **kw):
    return decode(to_jpeg(render_scene(item, **kw)))


def test_crop_and_rotate():
    a = img()
    assert a.shape[:2] == (720, 1280)
    c = crop(a, CropRegion(x=0.25, y=0.1, w=0.5, h=0.8))
    assert c.shape[:2] == (576, 640)
    r = rotate(a, 90)
    assert r.shape[:2] == (1280, 720)
    p = prepare(to_jpeg(render_scene(DEMO_ITEMS[0])), 270, CropRegion(x=0, y=0, w=1, h=0.5))
    assert p.shape[:2] == (640, 720)


def test_blur_is_detected():
    sharp = measure_quality(img())
    blurry = measure_quality(img(blur=6))
    assert sharp.sharpness > 60 > blurry.sharpness
    assert sharp.acceptable(60, 35, 235)
    assert "blurry" in blurry.problems(60, 35, 235)


def test_dark_frame_rejected():
    dark = (img().astype(np.float32) * 0.1).astype(np.uint8)
    assert "too dark" in measure_quality(dark).problems(60, 35, 235)


# -- quality is judged on the game, not the backdrop around it ------------------------------

def _backdrop(level, seed=3, w=1280, h=720, sigma=3.0):
    rng = np.random.default_rng(seed)
    return np.clip(np.full((h, w, 3), level, np.float32) + rng.normal(0, sigma, (h, w, 3)), 0, 255).astype(np.uint8)


def _cartridge(w, h, body, label, exposure=1.0):
    """A cartridge (plastic body) with a printed label: art block and lines of text."""
    import cv2

    g = np.full((h, w, 3), body, np.uint8)
    lab = np.full((int(h * 0.55), int(w * 0.8), 3), label, np.uint8)
    cv2.rectangle(lab, (10, 10), (lab.shape[1] - 10, lab.shape[0] // 2), (40, 60, 200), -1)
    for k in range(4):
        cv2.putText(lab, "SONIC 2", (14, 30 + 22 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (20, 20, 20), 2)
    g[int(h * 0.1):int(h * 0.1) + lab.shape[0], int(w * 0.1):int(w * 0.1) + lab.shape[1]] = lab
    return (g.astype(np.float32) * exposure).astype(np.uint8)


def _on(backdrop, item, x=400, y=150, blur=0.0):
    import cv2

    out = backdrop.copy()
    out[y:y + item.shape[0], x:x + item.shape[1]] = item
    if blur:
        out = cv2.GaussianBlur(out, (0, 0), blur)
    return decode(to_jpeg_bgr(out))


def to_jpeg_bgr(bgr) -> bytes:
    import cv2

    return cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


def test_lit_game_on_black_backdrop_is_not_too_dark():
    """Operator report: a lit game on a black backdrop was rejected as 'too dark'."""
    for size in [(300, 200), (480, 320), (700, 470)]:
        for body, label in [(40, 170), (25, 120), (110, 200)]:
            q = measure_quality(_on(_backdrop(10), _cartridge(*size, body, label)))
            assert q.problems(60, 35, 235) == [], (size, body, label, q.to_dict())
            assert q.brightness >= 50 and q.subject < 0.5


def test_underexposed_game_on_black_is_still_too_dark():
    q = measure_quality(_on(_backdrop(6), _cartridge(480, 320, 40, 170, exposure=0.2)))  # too little light
    assert "too dark" in q.problems(60, 35, 235)
    empty = measure_quality(decode(to_jpeg_bgr(_backdrop(8))))
    assert "too dark" in empty.problems(60, 35, 235) and empty.subject == 1.0


def test_blur_is_judged_on_the_game_itself():
    small = _cartridge(300, 200, 40, 170)
    assert measure_quality(_on(_backdrop(10), small, blur=1.0)).acceptable(60, 35, 235), "slight blur is still readable"
    assert "blurry" in measure_quality(_on(_backdrop(10), small, blur=3.0)).problems(60, 35, 235)


def test_game_on_white_backdrop_is_not_too_bright():
    q = measure_quality(_on(_backdrop(240, sigma=2.0), _cartridge(480, 320, 110, 200)))
    assert q.problems(60, 35, 235) == [] and q.brightness < 200


def test_fingerprints_same_vs_different():
    from gameoverlay.config import DetectionSettings
    from gameoverlay.vision.imaging import Fingerprint

    d = DetectionSettings()
    base = img(t=0.0, jitter=0.0)
    a1 = fingerprint(base)
    rng = np.random.default_rng(7)
    same_variants = [
        img(t=0.3, jitter=0.05),
        img(t=0.7, jitter=0.15),
        np.clip(base.astype(int) * 1.1, 0, 255).astype(np.uint8),  # lighting drift
        np.clip(base.astype(int) + rng.integers(-12, 12, base.shape), 0, 255).astype(np.uint8),  # sensor noise
    ]
    for v in same_variants:
        assert fingerprint_distance(a1, fingerprint(v)) < d.same_item_threshold
    for other in DEMO_ITEMS[1:]:
        # includes the Player's Choice copy of the same game (different product)
        assert fingerprint_distance(a1, fingerprint(img(other))) > d.change_threshold
    assert fingerprint_distance(a1, fingerprint(img(None))) > d.change_threshold
    hx, hist = a1.to_record()
    assert fingerprint_distance(a1, Fingerprint.from_record(hx, hist)) < 0.01


def test_stability_detector_emits_once_per_presentation():
    det = ChangeDetector(motion_threshold=0.035, stable_frames_required=3)
    events = []
    frames = [img(None)] * 4 + [img(DEMO_ITEMS[0], t=t / 10, jitter=1.0) for t in range(4)] + [img(DEMO_ITEMS[0])] * 6
    for f in frames:
        events.append(det.update(f).became_stable)
    assert events.count(True) == 2  # initial calm scene, then after the game settles
    assert det.is_stable
