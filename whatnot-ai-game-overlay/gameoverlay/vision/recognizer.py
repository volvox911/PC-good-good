"""Claude Vision game identification (official Anthropic Python SDK).

* Uses ``client.beta.messages.parse`` with a Pydantic schema so every response is
  schema-validated (:class:`GameIdentification`).
* Server-side refusal fallbacks are enabled (``fallbacks="default"``) and a final
  ``stop_reason == "refusal"`` is treated as "not identified" — never as a guess.
* Every call is pre-checked against the per-session spend limit and per-minute
  request cap, and its token usage is recorded for cost reporting.
* Claude output is *never* used as a price; it only drives the PriceCharting search.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import anthropic

from ..config import RecognitionSettings
from .prompts import USER_INSTRUCTION_MULTI, USER_INSTRUCTION_SINGLE, system_prompt
from .schema import GameIdentification
from .usage import RequestRateExceeded, SpendLimitReached, UsageTracker

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class RecognitionOutcome:
    status: str  # ok | refused | invalid_output | not_configured | auth_error | rate_limited |
    #              network | api_error | spend_limit | request_cap
    result: GameIdentification | None = None
    error: str | None = None
    model: str | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0
    images: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.result is not None


class Recognizer(Protocol):
    async def recognize(self, images: list[bytes]) -> RecognitionOutcome: ...

    def status(self) -> dict: ...


class ClaudeRecognizer:
    def __init__(
        self,
        api_key: Callable[[], str | None],
        settings: Callable[[], RecognitionSettings],
        usage: UsageTracker,
        client_factory: Callable[[str, RecognitionSettings], object] | None = None,
    ) -> None:
        self._api_key = api_key
        self._settings = settings
        self.usage = usage
        self._factory = client_factory or self._default_factory
        self._client = None
        self._client_key: tuple | None = None
        self.last_ok_at: float | None = None
        self.last_error: str | None = None
        self.last_status: str | None = None
        self.last_latency_ms: int | None = None

    @staticmethod
    def _default_factory(key: str, cfg: RecognitionSettings):
        return anthropic.AsyncAnthropic(api_key=key, timeout=cfg.request_timeout_s, max_retries=1)

    def _get_client(self, key: str, cfg: RecognitionSettings):
        ck = (key, cfg.request_timeout_s)
        if self._client is None or self._client_key != ck:
            self._client = self._factory(key, cfg)
            self._client_key = ck
        return self._client

    def status(self) -> dict:
        configured = bool(self._api_key())
        if not configured:
            state = "not_configured"
        elif self.last_status in ("auth_error",):
            state = "auth_failed"
        elif self.last_status in ("network", "api_error", "rate_limited"):
            state = "error"
        elif self.last_status in ("spend_limit",):
            state = "limit_reached"
        elif self.last_ok_at:
            state = "connected"
        else:
            state = "idle"
        return {
            "state": state,
            "lastOkAt": self.last_ok_at,
            "lastError": self.last_error,
            "lastLatencyMs": self.last_latency_ms,
            "model": self._settings().model,
            "usage": self.usage.snapshot().to_dict(),
        }

    def _finish(self, out: RecognitionOutcome) -> RecognitionOutcome:
        self.last_status = out.status
        if out.ok:
            self.last_ok_at = time.time()
            self.last_error = None
        elif out.error:
            self.last_error = out.error
        self.last_latency_ms = out.latency_ms or self.last_latency_ms
        return out

    async def recognize(self, images: list[bytes]) -> RecognitionOutcome:
        cfg = self._settings()
        key = self._api_key()
        if not key:
            return self._finish(RecognitionOutcome("not_configured", error="Anthropic API key is not configured"))
        if not images:
            return self._finish(RecognitionOutcome("invalid_output", error="no images"))
        try:
            self.usage.check_allowed()
        except SpendLimitReached as e:
            return self._finish(RecognitionOutcome("spend_limit", error=str(e)))
        except RequestRateExceeded as e:
            return self._finish(RecognitionOutcome("request_cap", error=str(e)))

        content: list[dict] = []
        for img in images:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(img).decode()},
                }
            )
        text = USER_INSTRUCTION_SINGLE if len(images) == 1 else USER_INSTRUCTION_MULTI.format(n=len(images))
        content.append({"type": "text", "text": text})

        kwargs: dict = dict(
            model=cfg.model,
            max_tokens=8000,
            system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config={"effort": cfg.effort},
            output_format=GameIdentification,
        )
        if cfg.use_server_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        client = self._get_client(key, cfg)
        t0 = time.monotonic()
        try:
            resp = await client.beta.messages.parse(**kwargs)
        except anthropic.AuthenticationError:
            return self._fail("auth_error", "Anthropic rejected the API key", cfg, t0, len(images))
        except anthropic.PermissionDeniedError:
            return self._fail("auth_error", "Anthropic API key lacks permission for this model", cfg, t0, len(images))
        except anthropic.RateLimitError:
            return self._fail("rate_limited", "Anthropic rate limit reached", cfg, t0, len(images))
        except anthropic.APITimeoutError:
            return self._fail("network", "Anthropic request timed out", cfg, t0, len(images))
        except anthropic.APIConnectionError:
            return self._fail("network", "Could not reach the Anthropic API", cfg, t0, len(images))
        except anthropic.BadRequestError as e:
            return self._fail("api_error", f"Anthropic rejected the request: {_short(e)}", cfg, t0, len(images))
        except anthropic.APIStatusError as e:
            return self._fail("api_error", f"Anthropic API error HTTP {e.status_code}", cfg, t0, len(images))
        except Exception as e:  # pydantic validation of the parsed output, etc.
            log.warning("recognition output could not be parsed: %s", type(e).__name__)
            return self._fail("invalid_output", "model output failed schema validation", cfg, t0, len(images))

        latency = int((time.monotonic() - t0) * 1000)
        u = getattr(resp, "usage", None)
        model_used = getattr(resp, "model", None) or cfg.model
        cost = self.usage.record(
            model_used,
            "ok" if resp.stop_reason != "refusal" else "refused",
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )
        if resp.stop_reason == "refusal":
            return self._finish(
                RecognitionOutcome("refused", error="model declined the request", model=model_used,
                                   latency_ms=latency, cost_usd=cost, images=len(images))
            )
        if resp.stop_reason == "max_tokens":
            return self._finish(
                RecognitionOutcome("invalid_output", error="response truncated", model=model_used,
                                   latency_ms=latency, cost_usd=cost, images=len(images))
            )
        parsed = getattr(resp, "parsed_output", None)
        if not isinstance(parsed, GameIdentification):
            return self._finish(
                RecognitionOutcome("invalid_output", error="no structured output", model=model_used,
                                   latency_ms=latency, cost_usd=cost, images=len(images))
            )
        return self._finish(
            RecognitionOutcome("ok", result=sanitize(parsed), model=model_used, latency_ms=latency,
                               cost_usd=cost, images=len(images))
        )

    def _fail(self, status: str, msg: str, cfg: RecognitionSettings, t0: float, n: int) -> RecognitionOutcome:
        self.usage.record(cfg.model, status)
        log.warning("recognition failed: %s", msg)
        return self._finish(
            RecognitionOutcome(status, error=msg, model=cfg.model, latency_ms=int((time.monotonic() - t0) * 1000), images=n)
        )


def _short(e: Exception) -> str:
    return str(getattr(e, "message", e))[:200]


def sanitize(r: GameIdentification) -> GameIdentification:
    """Trim/limit free-text fields defensively (model output is untrusted input)."""

    def s(v: str | None, n: int = 200) -> str | None:
        if v is None:
            return None
        v = " ".join(str(v).split())[:n]
        return v or None

    return r.model_copy(
        update={
            "title": s(r.title),
            "alternate_titles": [t for t in (s(x) for x in r.alternate_titles[:3]) if t],
            "platform": s(r.platform, 60),
            "platform_evidence": s(r.platform_evidence, 300) or "",
            "region_evidence": s(r.region_evidence, 300),
            "edition": s(r.edition, 80),
            "edition_evidence": s(r.edition_evidence, 300),
            "visible_text": [t for t in (s(x, 120) for x in r.visible_text[:12]) if t],
            "upc_digits": "".join(ch for ch in (r.upc_digits or "") if ch.isdigit())[:14] or None,
        }
    )
