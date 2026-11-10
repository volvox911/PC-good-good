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
