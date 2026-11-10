"""Recognition state machine.

States: IDLE, DETECTING, RECOGNIZING, MATCHING, PRICE_LOOKUP, DISPLAYING,
NEEDS_CONFIRMATION, ERROR, PAUSED.

Flow per presented item::

    sample (small screenshot) -> motion/stability -> stable frame fingerprint
      -> same as displayed?  -> nothing (no API calls)
      -> empty background?   -> hide after empty grace
      -> new item            -> job(generation N):
            HQ burst + quality check -> visual memory? -> Claude Vision
            -> PriceCharting candidate validation -> prices (cache/refresh)
            -> commit VerifiedRecord atomically (only if N is still current)

Every job carries a generation number. Any newer presentation, manual selection,
clear or recall increments the generation; superseded jobs are cancelled and, if
an old response still arrives, it is discarded before it can reach the overlay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

from ..config import RuntimeSettings
from ..db import Database
from ..models import Candidate, PriceLookup, VerifiedRecord
from ..money import format_usd
from ..obs.client import OBSClient, OBSError
from ..overlay.hub import OverlayHub
from ..pricing.client import PriceChartingAuthError, PriceChartingError
from ..pricing.service import PriceUnavailable, PricingService
from ..vision.change import ChangeDetector
from ..vision.imaging import (
    Fingerprint,
    encode_jpeg,
    fingerprint,
    fingerprint_distance,
    limit_long_side,
    measure_quality,
    prepare,
)
from ..vision.recognizer import RecognitionOutcome, Recognizer
from ..vision.schema import GameIdentification
from .matcher import Matcher, MatchResult

log = logging.getLogger(__name__)


DRIFT_CHECK_S = 1.5


class PipelineState(StrEnum):
    IDLE = "idle"
    DETECTING = "detecting"
    RECOGNIZING = "recognizing"
    MATCHING = "matching"
    PRICE_LOOKUP = "price_lookup"
    DISPLAYING = "displaying"
    NEEDS_CONFIRMATION = "needs_confirmation"
    ERROR = "error"
    PAUSED = "paused"


class Superseded(Exception):
    """A newer presentation/operator action replaced this job."""


@dataclass
class CurrentItem:
    """What the pipeline knows about the item currently presented (dashboard only)."""

    generation: int = 0
    started_at: float = 0.0
    status: str = "Waiting for a game"
    error: str | None = None
    quality: dict | None = None
    recognition: dict | None = None
    recognition_meta: dict | None = None
    match: dict | None = None
    prices: dict | None = None
    freshness: str | None = None
    stale_prices: dict | None = None
    timings_ms: dict = field(default_factory=dict)
    match_key: str | None = None
    frame_jpeg: bytes | None = None

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "startedAt": self.started_at,
            "status": self.status,
            "error": self.error,
            "quality": self.quality,
            "recognition": self.recognition,
            "recognitionMeta": self.recognition_meta,
            "match": self.match,
            "prices": self.prices,
            "freshness": self.freshness,
            "stalePrices": self.stale_prices,
            "timingsMs": self.timings_ms,
            "hasFrame": self.frame_jpeg is not None,
        }


class Engine:
    def __init__(
        self,
        *,
        obs: OBSClient,
        recognizer: Recognizer,
        matcher: Matcher,
        pricing: PricingService,
        hub: OverlayHub,
        db: Database,
        settings: Callable[[], RuntimeSettings],
        session_id: str,
        demo: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.obs = obs
        self.recognizer = recognizer
        self.matcher = matcher
        self.pricing = pricing
        self.hub = hub
        self.db = db
        self._settings = settings
        self.session_id = session_id
        self.demo = demo
        self._clock = clock

        s = settings()
        self.detector = ChangeDetector(s.detection.motion_threshold, s.detection.stable_frames_required)
        self.state = PipelineState.IDLE
        self.detection_enabled = True
        self.pinned = False
        self.state_reason: str | None = None
        self._transient_error = False
        self.pending_record: VerifiedRecord | None = None  # resolved while the overlay was held
        self.current = CurrentItem()
        self.last_motion = 0.0
        self.samples = 0
        self.jobs_started = 0
        self.jobs_superseded = 0

        self._generation = 0
        self._job: asyncio.Task | None = None
        self._job_fp: Fingerprint | None = None
        self._displayed_fp: Fingerprint | None = None
        self._last_attempt_fp: Fingerprint | None = None
        self._empty_fp: Fingerprint | None = None
        self._last_stable_fp: Fingerprint | None = None
        self._hide_deadline: float | None = None
        self._hide_reason: str | None = None
        self._displayed_at: float | None = None  # engine clock
        self._empty_since: float | None = None
        self._memory: deque[tuple[Fingerprint, str]] = deque(maxlen=400)
        self._loop_task: asyncio.Task | None = None
        self._running = False
        self._listeners: list[Callable[[], None]] = []
        self._capture_lock = asyncio.Lock()
        self._last_drift_check = 0.0
        self._pending_fp: Fingerprint | None = None
        self._age_task: asyncio.Task | None = None
        self._last_age_attempt = 0.0
        self._wall: Callable[[], float] = time.time  # wall clock for price ages (overridable in tests)
        self._load_background()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._running = True
            self._loop_task = asyncio.create_task(self._loop(), name="pipeline-loop")

    async def stop(self) -> None:
        self._running = False
        for t in (self._job, self._loop_task, self._age_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._job = None
        self._loop_task = None

    def on_change(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # pragma: no cover
                log.exception("engine listener failed")

    def _set_state(self, state: PipelineState, reason: str | None = None, transient: bool = False) -> None:
        self._transient_error = transient and state in (PipelineState.ERROR, PipelineState.IDLE)
        if state != self.state or reason != self.state_reason:
            self.state = state
            self.state_reason = reason
            self._notify()

    @property
    def settings(self) -> RuntimeSettings:
        return self._settings()

    @property
    def generation(self) -> int:
        return self._generation

    def _check_gen(self, gen: int) -> None:
        if gen != self._generation:
            raise Superseded()

    # ------------------------------------------------------------------ main loop
    async def _loop(self) -> None:
        while self._running:
            t0 = time.monotonic()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except OBSError as e:
                self._set_state(PipelineState.ERROR, f"Camera capture failed: {e}", transient=True)
            except Exception as e:  # never let the loop die during a live show
                log.exception("pipeline tick failed")
                self._set_state(PipelineState.ERROR, f"Internal error: {type(e).__name__}", transient=True)
            interval = self.settings.detection.sample_interval_ms / 1000.0
            await asyncio.sleep(max(0.02, interval - (time.monotonic() - t0)))

    def _check_timers(self) -> None:
        now = self._clock()
        d = self.settings.display
        if not self.hub.visible or self.pinned:
            return
        if self._hide_deadline is not None and now >= self._hide_deadline:
            self._hide(self._hide_reason or "unidentified item")
        elif self._empty_since is not None and d.empty_grace_s > 0 and now - self._empty_since >= d.empty_grace_s:
            self._hide("presentation area empty")
        elif d.display_timeout_s > 0 and self._displayed_at and now - self._displayed_at >= d.display_timeout_s:
            self._hide("display timeout")

    def _check_on_air_age(self) -> None:
        """Never leave prices on air past the refresh interval without re-validating them."""
        rec = self.hub.current if self.hub.visible else None
        if rec is None or (self._age_task is not None and not self._age_task.done()):
            return
        if self._wall() - rec.retrieved_at < self.settings.pricing.price_refresh_s:
            return
        if time.monotonic() - self._last_age_attempt < 30:
            return
        self._last_age_attempt = time.monotonic()
        self._age_task = asyncio.create_task(self._refresh_on_air(rec, self._generation), name="on-air-refresh")

    async def _refresh_on_air(self, rec: VerifiedRecord, gen: int) -> None:
        still_on_air = lambda: (  # noqa: E731
            gen == self._generation and self.hub.visible and self.hub.current is not None
            and self.hub.current.product_id == rec.product_id
        )
        try:
            lookup = await self.pricing.get_prices(rec.product_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if still_on_air():
                log.warning("on-air prices for %s could not be refreshed (%s); hiding", rec.product_id, e)
                self.current.error = str(e)
                self._hide("on-air prices expired and could not be refreshed")
            return
        if still_on_air():
            new = VerifiedRecord.from_prices(lookup.prices, lookup.freshness, rec.source, demo=rec.demo)
            if (new.loose_cents, new.cib_cents, new.new_cents, new.freshness, new.retrieved_at) != (
                rec.loose_cents, rec.cib_cents, rec.new_cents, rec.freshness, rec.retrieved_at
            ):
                self.hub.show(new)
                self._notify()

    def _hide(self, reason: str) -> None:
        log.info("hiding overlay: %s", reason)
        self.hub.hide(reason)
        self._displayed_fp = None
        self._displayed_at = None
        self._hide_deadline = None
        self._empty_since = None
        if self.state == PipelineState.DISPLAYING:
            self._set_state(PipelineState.DETECTING)
        self._notify()

    async def _tick(self) -> None:
        self._check_timers()
        self._check_on_air_age()
        s = self.settings
        self.detector.configure(s.detection.motion_threshold, s.detection.stable_frames_required)
        if not self.detection_enabled:
            if self.state not in (PipelineState.PAUSED,):
                self._set_state(PipelineState.PAUSED, "Detection paused by operator")
            return
        if not self.obs.connected:
            if self._job is None or self._job.done():
                self._set_state(PipelineState.IDLE, "Waiting for OBS connection", transient=True)
            return
        source = s.obs.camera_source
        if not source:
            self._set_state(PipelineState.IDLE, "Select the camera source in Settings", transient=True)
            return
        async with self._capture_lock:
            data = await self.obs.screenshot(source, width=s.obs.motion_sample_width, quality=70)
        img = prepare(data, s.obs.rotation, s.obs.crop)
        self.samples += 1
        upd = self.detector.update(img)
        self.last_motion = upd.motion
        if (self.state == PipelineState.PAUSED or self._transient_error) and (self._job is None or self._job.done()):
            # Recovered from "waiting for OBS"/capture hiccups. Job errors stay visible to
            # the operator until the next item or action.
            self._set_state(PipelineState.DISPLAYING if self.hub.visible else PipelineState.DETECTING)
        if upd.became_stable:
            self._last_drift_check = time.monotonic()
            await self._on_stable(fingerprint(img))
        elif self.detector.is_stable and time.monotonic() - self._last_drift_check >= DRIFT_CHECK_S:
            # Safety net for swaps with almost no visible motion: periodically compare the
            # calm scene with the last stable fingerprint.
            self._last_drift_check = time.monotonic()
            fp = fingerprint(img)
            thr = s.detection.change_threshold
            ref = self._last_stable_fp
            moved = ref is not None and fingerprint_distance(fp, ref) >= thr
            # Also re-check when the calm scene no longer matches what is on air (e.g. after a
            # hold is released, or a superseded result). _on_stable ignores scenes already handled.
            off_air = (
                self.hub.visible and not self.pinned and self._displayed_fp is not None
                and fingerprint_distance(fp, self._displayed_fp) >= thr
            )
            if moved or off_air:
                await self._on_stable(fp)

    # ------------------------------------------------------------------ presentation logic
    async def _on_stable(self, fp: Fingerprint) -> None:
        d = self.settings.detection
        disp = self.settings.display
        self._last_stable_fp = fp
        if self._job is not None and not self._job.done():
            if self._job_fp is not None and fingerprint_distance(fp, self._job_fp) < d.change_threshold:
                return  # still the item being identified
            # The item being identified has left the camera: its result must never be shown.
            self._bump_generation()
            self._job_fp = None
            if self.state in (PipelineState.RECOGNIZING, PipelineState.MATCHING, PipelineState.PRICE_LOOKUP):
                self._set_state(PipelineState.DISPLAYING if self.hub.visible else PipelineState.DETECTING)
        if self._displayed_fp is not None and fingerprint_distance(fp, self._displayed_fp) < d.change_threshold:
            # The displayed game is still (or again) in front of the camera.
            self._hide_deadline = None
            self._empty_since = None
            if self.hub.visible and self.state != PipelineState.DISPLAYING:
                self._set_state(PipelineState.DISPLAYING)
            return
        if self._empty_fp is not None and fingerprint_distance(fp, self._empty_fp) < d.change_threshold:
            self._on_empty()
            return
        if self._last_attempt_fp is not None and fingerprint_distance(fp, self._last_attempt_fp) < d.change_threshold:
            return  # same unidentifiable (or held) scene as last time: don't pay for another identical call
        # A clearly different item: start a job; keep the old record only for the grace period.
        if self.hub.visible and not self.pinned:
            self._hide_deadline = self._clock() + disp.unidentified_grace_s
            self._hide_reason = "different item presented (unidentified)"
        self._start_job(fp)

    def _on_empty(self) -> None:
        if self.hub.visible and self._empty_since is None:
            self._empty_since = self._clock()
        self._hide_deadline = None
        if self._job and not self._job.done():
            self._bump_generation()
        self.current = CurrentItem(generation=self._generation, started_at=self._clock(), status="Presentation area empty")
        if not self.hub.visible:
            self._set_state(PipelineState.DETECTING)
        self._notify()

    def _bump_generation(self) -> int:
        self._generation += 1
        if self._job is not None and not self._job.done():
            self._job.cancel()
            self.jobs_superseded += 1
        return self._generation

    def _start_job(self, fp: Fingerprint | None, force: bool = False) -> None:
        gen = self._bump_generation()
        self._job_fp = fp
        self.jobs_started += 1
        self.current = CurrentItem(generation=gen, started_at=self._clock(), status="New item detected")
        self._job = asyncio.create_task(self._run_job(gen, fp), name=f"job-{gen}")

    # ------------------------------------------------------------------ job
    async def _capture_stills(self, n: int) -> list[tuple[object, object]]:
        s = self.settings
        out = []
        for i in range(n):
            async with self._capture_lock:
                data = await self.obs.screenshot(
                    s.obs.camera_source, width=s.obs.still_max_width or None, quality=s.obs.still_jpeg_quality
                )
            img = prepare(data, s.obs.rotation, s.obs.crop)
            out.append((measure_quality(img), img))
            if i < n - 1:
                await asyncio.sleep(s.detection.burst_spacing_ms / 1000)
        return out

    def _encode_for_model(self, img) -> bytes:
        return encode_jpeg(limit_long_side(img, self.settings.recognition.max_image_long_side), 90)

    async def _run_job(self, gen: int, fp: Fingerprint | None) -> None:
        s = self.settings
        t_start = time.monotonic()
        cur = self.current
        try:
            self._set_state(PipelineState.RECOGNIZING, "Capturing high-quality image")
            stills = await self._capture_stills(s.detection.burst_frames)
            self._check_gen(gen)
            dq = s.detection
            good = [x for x in stills if x[0].acceptable(dq.min_sharpness, dq.min_brightness, dq.max_brightness)]
            if not good:
                await asyncio.sleep(0.4)
                stills += await self._capture_stills(1)
                self._check_gen(gen)
                good = [x for x in stills if x[0].acceptable(dq.min_sharpness, dq.min_brightness, dq.max_brightness)]
            best_q, best = max(stills, key=lambda x: x[0].sharpness)
            cur.quality = best_q.to_dict()
            cur.frame_jpeg = encode_jpeg(limit_long_side(best, 640), 80)
            cur.timings_ms["capture"] = int((time.monotonic() - t_start) * 1000)
            if not good:
                problems = ", ".join(best_q.problems(dq.min_sharpness, dq.min_brightness, dq.max_brightness))
                cur.status = f"Image quality too low ({problems}) — hold the game steady"
                self._set_state(PipelineState.DETECTING, cur.status)
                self.detector.rearm()  # try again on the next calm period
                return
            good.sort(key=lambda x: -x[0].sharpness)

            # Visual memory: the exact same item was identified earlier this session.
            if s.detection.visual_memory_enabled and fp is not None:
                hit = self._memory_lookup(fp)
                if hit:
                    cur.status = "Recognized from earlier this session"
                    cur.match = {"status": "matched", "via": "visual_memory", "product": None, "score": None,
                                 "reason": "visually identical to an earlier identified item", "suggestions": []}
                    await self._price_and_commit(gen, hit, "auto", fp, t_start)
                    return

            t_rec = time.monotonic()
            outcome = await self.recognizer.recognize([self._encode_for_model(good[0][1])])
            self._check_gen(gen)
            r = outcome.result
            if outcome.ok and r is not None and len(good) > 1 and (
                r.needs_more_images or (r.title_obscured and r.confidence != "high")
            ):
                cur.status = "Taking a second look"
                second = await self.recognizer.recognize(
                    [self._encode_for_model(good[0][1]), self._encode_for_model(good[1][1])]
                )
                self._check_gen(gen)
                if second.ok:
                    outcome = second
            cur.timings_ms["recognition"] = int((time.monotonic() - t_rec) * 1000)
            self._record_recognition(cur, outcome)
            if not outcome.ok:
                self._handle_recognition_failure(cur, outcome, fp)
                return
            rec = outcome.result

            self._set_state(PipelineState.MATCHING, "Validating against PriceCharting")
            t_match = time.monotonic()
            match = await self.matcher.resolve(rec)
            self._check_gen(gen)
            cur.timings_ms["matching"] = int((time.monotonic() - t_match) * 1000)
            cur.match = match.to_dict()
            cur.match_key = match.key

            if match.status == "not_a_game":
                self._last_attempt_fp = fp
                if not rec.item_present:
                    self._on_empty()
                else:
                    cur.status = f"Not a game: {match.reason}"
                    self._set_state(PipelineState.DISPLAYING if self.hub.visible else PipelineState.DETECTING, cur.status)
                return
            if match.status != "matched" or match.product is None:
                self._last_attempt_fp = fp
                cur.status = f"Needs confirmation: {match.reason}"
                self._set_state(PipelineState.NEEDS_CONFIRMATION, cur.status)
                return

            if self._needs_corroboration(rec, match):
                cur.status = "Corroborating before replacing the current game"
                ok = await self._corroborate(gen, match)
                self._check_gen(gen)
                if not ok:
                    self._last_attempt_fp = fp
                    cur.status = "Needs confirmation: second look disagreed"
                    self._set_state(PipelineState.NEEDS_CONFIRMATION, cur.status)
                    return

            await self._price_and_commit(gen, match.product.product_id, "auto", fp, t_start)
        except Superseded:
            pass
        except asyncio.CancelledError:
            pass
        except PriceUnavailable as e:
            if gen == self._generation:
                cur.error = str(e)
                cur.stale_prices = e.stale.to_dict() if e.stale else None
                cur.status = "Price refresh failed — not displayed (stale data is never shown as current)"
                self._set_state(PipelineState.ERROR, cur.status)
        except PriceChartingAuthError as e:
            if gen == self._generation:
                cur.error = str(e)
                cur.status = "PriceCharting authentication failed"
                self._set_state(PipelineState.ERROR, cur.status)
        except PriceChartingError as e:
            if gen == self._generation:
                cur.error = str(e)
                cur.status = "PriceCharting unavailable"
                self._set_state(PipelineState.ERROR, cur.status)
        except OBSError as e:
            if gen == self._generation:
                cur.error = str(e)
                cur.status = "Camera capture failed"
                self._set_state(PipelineState.ERROR, cur.status)
        except Exception as e:
            log.exception("recognition job failed")
            if gen == self._generation:
                cur.error = f"{type(e).__name__}"
                cur.status = "Internal error during recognition"
                self._set_state(PipelineState.ERROR, cur.status)
        finally:
            self._notify()

    def _record_recognition(self, cur: CurrentItem, outcome: RecognitionOutcome) -> None:
        cur.recognition = outcome.result.model_dump() if outcome.result else None
        cur.recognition_meta = {
            "status": outcome.status,
            "error": outcome.error,
            "model": outcome.model,
            "latencyMs": outcome.latency_ms,
            "costUsd": round(outcome.cost_usd, 5),
            "images": outcome.images,
        }

    def _handle_recognition_failure(self, cur: CurrentItem, outcome: RecognitionOutcome, fp) -> None:
        cur.error = outcome.error
        if outcome.status in ("spend_limit",):
            self.detection_enabled = False
            cur.status = "Claude spend limit reached — detection paused (manual search still works)"
            self._set_state(PipelineState.PAUSED, cur.status)
        elif outcome.status in ("not_configured", "auth_error"):
            cur.status = f"Claude unavailable: {outcome.error}"
            self._set_state(PipelineState.ERROR, cur.status)
        elif outcome.status == "refused":
            self._last_attempt_fp = fp
            cur.status = "Could not identify this item — use manual search"
            self._set_state(PipelineState.NEEDS_CONFIRMATION, cur.status)
        else:
            cur.status = f"Recognition failed ({outcome.status}) — will retry on next presentation"
            self._set_state(PipelineState.ERROR, cur.status)
            self.detector.rearm()

    def _needs_corroboration(self, rec: GameIdentification, match: MatchResult) -> bool:
        cfg = self.settings.recognition
        if not cfg.require_corroboration or not self.hub.visible or self.hub.current is None:
            return False
        if match.product and match.product.product_id == self.hub.current.product_id:
            return False
        if match.via in ("match_cache", "upc"):
            return rec.confidence != "high"
        return rec.confidence != "high" or (match.score or 0) < 95

    async def _corroborate(self, gen: int, match: MatchResult) -> bool:
        stills = await self._capture_stills(1)
        self._check_gen(gen)
        q, img = stills[0]
        outcome = await self.recognizer.recognize([self._encode_for_model(img)])
        self._check_gen(gen)
        if not outcome.ok:
            return False
        second = await self.matcher.resolve(outcome.result)
        return second.status == "matched" and second.product is not None and second.product.product_id == match.product.product_id

    async def _price_and_commit(self, gen: int, product_id: str, source: str, fp, t_start: float, force: bool = False) -> None:
        cur = self.current
        self._set_state(PipelineState.PRICE_LOOKUP, "Retrieving PriceCharting values")
        t_p = time.monotonic()
        lookup = await self.pricing.get_prices(product_id, force_refresh=force)
        self._check_gen(gen)
        cur.timings_ms["prices"] = int((time.monotonic() - t_p) * 1000)
        # Anti-flicker: automatic replacements respect a minimum display time.
        same_product = self.hub.visible and self.hub.current is not None and self.hub.current.product_id == product_id
        if source == "auto" and self.hub.visible and self._displayed_at and not same_product:
            wait = self.settings.display.min_display_s - (self._clock() - self._displayed_at)
            if wait > 0:
                await asyncio.sleep(min(wait, self.settings.display.min_display_s))
                self._check_gen(gen)
        self._commit(gen, lookup, source, fp)
        cur.timings_ms["total"] = int((time.monotonic() - t_start) * 1000)

    def _commit(self, gen: int, lookup: PriceLookup, source: str, fp, remember_fp: Fingerprint | None = None) -> None:
        """Put one product on air atomically.

        ``fp`` is the scene fingerprint used for change detection. ``remember_fp`` (automatic
        identifications and explicit operator corrections only) teaches visual memory.
        """
        self._check_gen(gen)
        if source == "auto" and remember_fp is None:
            remember_fp = fp
        p = lookup.prices
        record = VerifiedRecord.from_prices(p, lookup.freshness, "demo" if self.demo and source == "auto" else source, demo=self.demo)
        cur = self.current
        cur.prices = p.to_dict()
        cur.freshness = lookup.freshness
        shown = self.hub.current if self.hub.visible else None
        if source == "auto" and shown is not None and shown.product_id == record.product_id and (
            shown.loose_cents, shown.cib_cents, shown.new_cents, shown.freshness
        ) == (record.loose_cents, record.cib_cents, record.new_cents, record.freshness):
            # Same game re-identified after a re-pose: keep it on screen untouched.
            self._displayed_fp = fp if fp is not None else self._displayed_fp
            self._hide_deadline = None
            self._empty_since = None
            self._last_attempt_fp = None
            cur.status = f"Displaying {p.product_name} ({p.console_name})"
            self._set_state(PipelineState.DISPLAYING, None)
            return
        if self.pinned and source == "auto":
            self.pending_record = record
            self._pending_fp = fp
            self._last_attempt_fp = fp  # already handled: don't re-identify it every few seconds
            cur.status = f"Held — {p.product_name} resolved but not shown (overlay is held)"
            self._notify()
            return
        self.hub.show(record)  # atomic: title + platform + 3 prices of one product
        self._displayed_at = self._clock()
        self._displayed_fp = fp if fp is not None else self._last_stable_fp
        self._hide_deadline = None
        self._empty_since = None
        self._last_attempt_fp = None
        self.pending_record = None
        self._pending_fp = None
        cur.status = f"Displaying {p.product_name} ({p.console_name})"
        if remember_fp is not None:
            self._memory_store(remember_fp, p.product_id)
        self._add_history(record, p.console_name, source, lookup.freshness)
        self._set_state(PipelineState.DISPLAYING, None)

    # ------------------------------------------------------------------ visual memory
    def _memory_lookup(self, fp: Fingerprint) -> str | None:
        thr = self.settings.detection.same_item_threshold
        best, best_d = None, 1.0
        for mfp, pid in self._memory:
            dd = fingerprint_distance(fp, mfp)
            if dd < best_d:
                best, best_d = pid, dd
        return best if best is not None and best_d < thr else None

    def _memory_store(self, fp: Fingerprint, product_id: str) -> None:
        for mfp, pid in list(self._memory)[-25:]:
            if pid == product_id and fingerprint_distance(fp, mfp) < 0.03:
                return  # already remembered (e.g. recognized via visual memory again)
        self._memory.append((fp, product_id))
        hx, hist = fp.to_record()
        self.db.execute(
            "INSERT INTO fingerprints(session_id, product_id, hash_hex, hist_json, created_at) VALUES (?,?,?,?,?)",
            (self.session_id, product_id, hx, json.dumps(hist), self._clock()),
        )

    def forget_visual_memory(self, product_id: str | None = None) -> None:
        if product_id is None:
            self._memory.clear()
        else:
            self._memory = deque(((f, p) for f, p in self._memory if p != product_id), maxlen=400)

    # ------------------------------------------------------------------ history
    def _add_history(self, record: VerifiedRecord, console: str, source: str, freshness: str) -> None:
        rec = self.current.recognition or {}
        self.db.execute(
            "INSERT INTO history(session_id, product_id, product_name, console_name, loose_cents, cib_cents, new_cents, "
            "price_retrieved_at, freshness, shown_at, source, recognized_title, recognized_platform, lookup_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, record.product_id, record.title, console, record.loose_cents, record.cib_cents,
                record.new_cents, record.retrieved_at, freshness, self._clock(), source,
                rec.get("title"), rec.get("platform"), "ok",
            ),
        )

    def history(self, limit: int = 100) -> list[dict]:
        rows = self.db.query("SELECT * FROM history ORDER BY id DESC LIMIT ?", (max(1, min(1000, limit)),))
        return [
            {
                "id": r["id"],
                "productId": r["product_id"],
                "productName": r["product_name"],
                "consoleName": r["console_name"],
                "loose": format_usd(r["loose_cents"]),
                "complete": format_usd(r["cib_cents"]),
                "new": format_usd(r["new_cents"]),
                "shownAt": r["shown_at"],
                "priceRetrievedAt": r["price_retrieved_at"],
                "freshness": r["freshness"],
                "source": r["source"],
                "recognizedTitle": r["recognized_title"],
                "recognizedPlatform": r["recognized_platform"],
                "currentSession": r["session_id"] == self.session_id,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ background reference
    def _load_background(self) -> None:
        bg = self.db.get_setting("empty_background_fp")
        if bg:
            try:
                self._empty_fp = Fingerprint.from_record(bg["hash"], bg["hist"])
            except Exception:
                self._empty_fp = None

    async def capture_background(self) -> None:
        s = self.settings
        if not s.obs.camera_source:
            raise ValueError("no camera source selected")
        async with self._capture_lock:
            data = await self.obs.screenshot(s.obs.camera_source, width=s.obs.motion_sample_width, quality=70)
        fp = fingerprint(prepare(data, s.obs.rotation, s.obs.crop))
        hx, hist = fp.to_record()
        self.db.set_setting("empty_background_fp", {"hash": hx, "hist": hist, "capturedAt": self._clock()})
        self._empty_fp = fp
        self._notify()

    def clear_background(self) -> None:
        self.db.set_setting("empty_background_fp", None)
        self._empty_fp = None
        self._notify()

    @property
    def has_background(self) -> bool:
        return self._empty_fp is not None

    # ------------------------------------------------------------------ operator actions
    def pause(self) -> None:
        self.detection_enabled = False
        self._bump_generation()
        self._set_state(PipelineState.PAUSED, "Detection paused by operator")

    def resume(self) -> None:
        self.detection_enabled = True
        self.detector.rearm()
        self._last_attempt_fp = None
        self._set_state(PipelineState.DISPLAYING if self.hub.visible else PipelineState.DETECTING)

    def rescan(self) -> None:
        """Force a fresh identification of whatever is in front of the camera."""
        self._last_attempt_fp = None
        self._displayed_fp = None
        self._start_job(self._last_stable_fp)

    def clear_overlay(self) -> None:
        self._bump_generation()
        self.pinned = False
        self.hub.hide("cleared by operator")
        self._displayed_fp = None
        self._displayed_at = None
        self._hide_deadline = None
        self._empty_since = None
        self._last_attempt_fp = None
        self.pending_record = None
        self._set_state(PipelineState.DETECTING if self.detection_enabled else PipelineState.PAUSED)

    def hold(self, on: bool) -> None:
        self.pinned = bool(on)
        if on:
            self._hide_deadline = None
            self._empty_since = None
        self._notify()

    async def show_pending(self) -> bool:
        """Show the game that was resolved while the overlay was held.

        Prices are looked up again through the normal freshness policy, so a record that
        waited a long time is never shown as current.
        """
        rec = self.pending_record
        if rec is None:
            return False
        fp = self._pending_fp
        gen = self._bump_generation()
        self.pinned = False
        self.pending_record = None
        self._pending_fp = None
        lookup = await self.pricing.get_prices(rec.product_id)
        self._commit(gen, lookup, "auto", fp)
        return True

    async def select_product(
        self,
        product_id: str,
        source: str = "manual",
        correction: bool = False,
        generation: int | None = None,
    ) -> VerifiedRecord:
        """Operator picked a product (suggestion, search result, UPC or history).

        Only an explicit *correction* of the item currently being identified teaches the
        match cache and visual memory — and only if the dashboard was looking at that same
        item (``generation``). Showing an arbitrary product for a buyer teaches nothing.
        """
        pid = str(product_id).strip()
        if not pid.isdigit():
            raise ValueError("invalid product id")
        learn = bool(
            correction and source == "manual" and generation is not None
            and generation == self.current.generation and self.current.match_key
        )
        key = self.current.match_key if learn else None
        corrected_fp = self._job_fp if learn else None
        gen = self._bump_generation()
        cur = self.current
        cur.generation = gen
        cur.status = "Looking up selected product"
        self._set_state(PipelineState.PRICE_LOOKUP, cur.status)
        try:
            lookup = await self.pricing.get_prices(pid)
        except Exception as e:
            cur.error = str(e)
            cur.status = "Could not load prices for the selected product"
            if isinstance(e, PriceUnavailable) and e.stale:
                cur.stale_prices = e.stale.to_dict()
            self._set_state(PipelineState.ERROR, cur.status)
            raise
        self._check_gen(gen)
        p = lookup.prices
        if key:
            self.matcher.remember(key, Candidate(p.product_id, p.product_name, p.console_name), "manual")
        cur.match = {"status": "matched", "via": source, "product": Candidate(p.product_id, p.product_name, p.console_name).to_dict(),
                     "score": None, "reason": f"selected by operator ({source})", "suggestions": (cur.match or {}).get("suggestions", [])}
        self.pinned = False
        self._commit(gen, lookup, source, self._last_stable_fp, remember_fp=corrected_fp)
        if corrected_fp is not None:
            self._displayed_fp = corrected_fp
        return self.hub.current

    async def lookup_upc(self, upc: str) -> VerifiedRecord:
        p = await self.pricing.lookup_upc(upc)
        if p is None:
            raise LookupError("no PriceCharting product for that UPC")
        return await self.select_product(p.product_id, source="manual")

    async def recall(self, history_id: int) -> VerifiedRecord:
        row = self.db.query_one("SELECT product_id FROM history WHERE id=?", (int(history_id),))
        if row is None:
            raise LookupError("history entry not found")
        return await self.select_product(row["product_id"], source="recall")

    async def refresh_prices(self) -> VerifiedRecord | None:
        rec = self.hub.current
        if rec is None:
            return None
        gen = self._generation
        lookup = await self.pricing.get_prices(rec.product_id, force_refresh=True)
        if gen != self._generation or self.hub.current is None or self.hub.current.product_id != rec.product_id:
            return None  # something else is on screen now; never apply these prices to it
        new = VerifiedRecord.from_prices(lookup.prices, lookup.freshness, rec.source, demo=rec.demo)
        self.hub.show(new)
        self._displayed_at = self._clock()
        self.current.prices = lookup.prices.to_dict()
        self.current.freshness = lookup.freshness
        self._notify()
        return new

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self.state_reason,
            "detectionEnabled": self.detection_enabled,
            "held": self.pinned,
            "generation": self._generation,
            "motion": round(self.last_motion, 4),
            "stable": self.detector.is_stable,
            "samples": self.samples,
            "jobsStarted": self.jobs_started,
            "jobsSuperseded": self.jobs_superseded,
            "hasBackground": self.has_background,
            "visualMemorySize": len(self._memory),
            "hideInS": None if self._hide_deadline is None else max(0.0, round(self._hide_deadline - self._clock(), 1)),
            "pendingWhileHeld": self.pending_record.to_overlay_dict() if self.pending_record else None,
            "current": self.current.to_dict(),
        }

