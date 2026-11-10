"""Structured logging with secret redaction.

Logs go to the console and a rotating JSON-lines file in ``<data_dir>/logs``. A
redaction filter scrubs configured secret values and well-known token patterns
(Anthropic keys, ``t=`` query parameters, OBS auth strings) from every record.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import Callable

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?<=[?&]t=)[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)(?<=authentication['\"]: ['\"])[A-Za-z0-9+/=]{8,}"),
    re.compile(r"(?i)(?<=x-api-key: )\S+"),
]

REDACTED = "[REDACTED]"


class RedactingFilter(logging.Filter):
    def __init__(self, secret_source: Callable[[], list[str]] | None = None) -> None:
        super().__init__()
        self._secret_source = secret_source
        self._cache: tuple[float, list[str]] = (0.0, [])

    def _secrets(self) -> list[str]:
        if self._secret_source is None:
            return []
        now = time.monotonic()
        if now - self._cache[0] > 5.0:
            try:
                self._cache = (now, sorted(self._secret_source(), key=len, reverse=True))
            except Exception:
                self._cache = (now, [])
        return self._cache[1]

    def scrub(self, text: str) -> str:
        for s in self._secrets():
            if s and s in text:
                text = text.replace(s, REDACTED)
        for p in _PATTERNS:
            text = p.sub(REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        clean = self.scrub(msg)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        record.msg = clean
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str)


_filter: RedactingFilter | None = None


def setup_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    json_console: bool = False,
    secret_source: Callable[[], list[str]] | None = None,
) -> RedactingFilter:
    global _filter
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    _filter = RedactingFilter(secret_source)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        JsonFormatter()
        if json_console
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    console.addFilter(_filter)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "overlay.log.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(JsonFormatter())
        fh.addFilter(_filter)
        root.addHandler(fh)

    set_level(level)
    # Quiet noisy libraries; never let HTTP libraries log request URLs with tokens.
    for noisy in ("httpx", "httpcore", "httpx2", "websockets", "anthropic", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return _filter


def set_level(level: str) -> None:
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))


def log_event(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
