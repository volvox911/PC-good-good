"""Cheap local change/stability detection (no API calls).

Samples arrive every ``sample_interval_ms``. The detector reports motion and emits a
single ``stable`` event once the presentation area has been calm for
``stable_frames_required`` consecutive samples after having moved. Only stable
frames are ever considered for (paid) recognition.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .imaging import motion_frame, motion_score


@dataclass
class DetectorUpdate:
    motion: float
    moving: bool
    became_stable: bool


class ChangeDetector:
    def __init__(self, motion_threshold: float, stable_frames_required: int) -> None:
        self.motion_threshold = motion_threshold
        self.stable_frames_required = stable_frames_required
        self._prev: np.ndarray | None = None
        self._calm = 0
        self._armed = True  # emit 'stable' once at startup, then after each motion period
        self.last_motion = 0.0
        self.is_stable = False

    def configure(self, motion_threshold: float, stable_frames_required: int) -> None:
        self.motion_threshold = motion_threshold
        self.stable_frames_required = stable_frames_required

    def rearm(self) -> None:
        """Force a new 'stable' event at the next calm period (e.g. camera/crop changed)."""
        self._armed = True
        self._calm = 0
        self._prev = None

    def update(self, img: np.ndarray) -> DetectorUpdate:
        mf = motion_frame(img)
        motion = 0.0 if self._prev is None else motion_score(self._prev, mf)
        self._prev = mf
        self.last_motion = motion
        moving = motion >= self.motion_threshold
        became_stable = False
        if moving:
            self._calm = 0
            self._armed = True
            self.is_stable = False
        else:
            self._calm += 1
            if self._armed and self._calm >= self.stable_frames_required:
                self._armed = False
                self.is_stable = True
                became_stable = True
        return DetectorUpdate(motion=motion, moving=moving, became_stable=became_stable)
