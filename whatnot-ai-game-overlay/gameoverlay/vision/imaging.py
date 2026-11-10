"""Image utilities: decode, rotate, crop, quality metrics and perceptual fingerprints."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..config import CropRegion


class ImageDecodeError(ValueError):
    pass


def decode(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        raise ImageDecodeError("could not decode image")
    return img


def encode_jpeg(img: np.ndarray, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise ImageDecodeError("could not encode JPEG")
    return buf.tobytes()


def rotate(img: np.ndarray, degrees: int) -> np.ndarray:
    if degrees == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def crop(img: np.ndarray, region: CropRegion) -> np.ndarray:
    h, w = img.shape[:2]
    x0 = int(round(region.x * w))
    y0 = int(round(region.y * h))
    x1 = min(w, max(x0 + 8, int(round((region.x + region.w) * w))))
    y1 = min(h, max(y0 + 8, int(round((region.y + region.h) * h))))
    return img[y0:y1, x0:x1]


def prepare(data: bytes, rotation: int, region: CropRegion) -> np.ndarray:
    return crop(rotate(decode(data), rotation), region)


def limit_long_side(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    m = max(h, w)
    if m <= max_side:
        return img
    s = max_side / m
    return cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)


@dataclass(frozen=True)
class Quality:
    sharpness: float
    brightness: float
    contrast: float
    width: int
    height: int

    def acceptable(self, min_sharpness: float, min_brightness: float, max_brightness: float) -> bool:
        return (
            self.sharpness >= min_sharpness
            and min_brightness <= self.brightness <= max_brightness
            and self.contrast >= 12.0
            and min(self.width, self.height) >= 120
        )

    def problems(self, min_sharpness: float, min_brightness: float, max_brightness: float) -> list[str]:
        p = []
        if self.sharpness < min_sharpness:
            p.append("blurry")
        if self.brightness < min_brightness:
            p.append("too dark")
        if self.brightness > max_brightness:
            p.append("too bright")
        if self.contrast < 12.0:
            p.append("low contrast")
        if min(self.width, self.height) < 120:
            p.append("too small")
        return p

    def to_dict(self) -> dict:
        return {
            "sharpness": round(self.sharpness, 1),
            "brightness": round(self.brightness, 1),
            "contrast": round(self.contrast, 1),
            "width": self.width,
            "height": self.height,
        }


def measure_quality(img: np.ndarray) -> Quality:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Normalize scale so sharpness thresholds are resolution independent.
    scale = 640.0 / max(1, w)
    g = cv2.resize(gray, (640, max(1, int(h * scale))), interpolation=cv2.INTER_AREA) if w != 640 else gray
    sharp = float(cv2.Laplacian(g, cv2.CV_64F).var())
    return Quality(
        sharpness=sharp,
        brightness=float(gray.mean()),
        contrast=float(gray.std()),
        width=w,
        height=h,
    )


# -- motion -----------------------------------------------------------------------------------

def motion_frame(img: np.ndarray) -> np.ndarray:
    """Tiny, blurred grayscale frame used for frame-to-frame motion scoring."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(small, (5, 5), 0).astype(np.float32)


def motion_score(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 1.0
    return float(np.mean(np.abs(a - b)) / 255.0)


# -- fingerprints -------------------------------------------------------------------------------

FP_GRID = 4  # 4x4 cells
FP_TOP_CELLS = 4  # distance = mean of the most-changed cells (localized change)


@dataclass(frozen=True)
class Fingerprint:
    """Localized perceptual fingerprint.

    The frame is split into a 4x4 grid; each cell keeps a brightness-normalized 8x8
    grayscale thumbnail and a small HSV histogram. The distance between two frames
    is the mean distance of the most-changed cells, so swapping the item in hand
    registers strongly even when most of the frame is unchanged background, while
    small hand jitter, sensor noise and lighting drift stay low.
    """

    thumbs: np.ndarray  # float32[16, 8, 8]
    hists: np.ndarray  # float32[16, 16]

    def to_record(self) -> tuple[str, list[float]]:
        return (
            self.thumbs.astype(np.float16).tobytes().hex(),
            [round(float(v), 5) for v in self.hists.flatten().tolist()],
        )

    @classmethod
    def from_record(cls, hex_thumbs: str, hists: list[float]) -> "Fingerprint":
        n = FP_GRID * FP_GRID
        thumbs = np.frombuffer(bytes.fromhex(hex_thumbs), dtype=np.float16).astype(np.float32).reshape(n, 8, 8)
        return cls(thumbs=thumbs, hists=np.asarray(hists, dtype=np.float32).reshape(n, -1))


def fingerprint(img: np.ndarray) -> Fingerprint:
    h0, w0 = img.shape[:2]
    small = cv2.resize(img, (256, max(FP_GRID * 8, int(256 * h0 / max(1, w0)))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, w = gray.shape
    thumbs, hists = [], []
    for i in range(FP_GRID):
        for j in range(FP_GRID):
            y0, y1 = i * h // FP_GRID, (i + 1) * h // FP_GRID
            x0, x1 = j * w // FP_GRID, (j + 1) * w // FP_GRID
            c = cv2.resize(gray[y0:y1, x0:x1], (8, 8), interpolation=cv2.INTER_AREA)
            thumbs.append(c - c.mean())
            hh = cv2.calcHist([hsv[y0:y1, x0:x1]], [0, 1], None, [8, 2], [0, 180, 0, 256])
            hists.append(cv2.normalize(hh, None).flatten())
    return Fingerprint(thumbs=np.asarray(thumbs, dtype=np.float32), hists=np.asarray(hists, dtype=np.float32))


def fingerprint_distance(a: Fingerprint, b: Fingerprint) -> float:
    """0 (identical) .. 1 (unrelated)."""
    if a.thumbs.shape != b.thumbs.shape:
        return 1.0
    cells = []
    for k in range(a.thumbs.shape[0]):
        t = min(1.0, float(np.mean(np.abs(a.thumbs[k] - b.thumbs[k]))) / 48.0)
        bh = float(cv2.compareHist(a.hists[k], b.hists[k], cv2.HISTCMP_BHATTACHARYYA))
        cells.append(0.6 * t + 0.4 * min(1.0, max(0.0, bh)))
    cells.sort(reverse=True)
    return float(np.mean(cells[:FP_TOP_CELLS]))
