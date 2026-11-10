"""Credential storage.

Resolution order for each secret:

1. Environment variable (useful for CI and power users; never committed).
2. OS credential store via :mod:`keyring` (Windows Credential Manager on Windows).
3. Fallback file ``<data_dir>/secrets.json`` with owner-only permissions — used only
   when no OS keyring backend is available. The dashboard warns when this is in use.

Secret values are never returned to browser clients; the dashboard only learns
whether each secret is configured.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

KEYRING_SERVICE = "whatnot-ai-game-overlay"

SecretName = Literal["anthropic_api_key", "pricecharting_token", "obs_password"]

ENV_VARS: dict[str, tuple[str, ...]] = {
    "anthropic_api_key": ("ANTHROPIC_API_KEY",),
    "pricecharting_token": ("PRICECHARTING_TOKEN", "PRICECHARTING_API_TOKEN"),
    "obs_password": ("OBS_PASSWORD", "OBS_WEBSOCKET_PASSWORD"),
}

ALL_SECRETS: tuple[str, ...] = tuple(ENV_VARS)


@dataclass(frozen=True)
class SecretStatus:
    name: str
    configured: bool
    source: Literal["env", "keyring", "file", "none"]


class SecretStore:
    def __init__(self, data_dir: Path, use_keyring: bool = True) -> None:
        self._file = data_dir / "secrets.json"
        self._lock = threading.Lock()
        self._keyring = None
        if use_keyring:
            try:
                import keyring
                from keyring.backends import fail

                kr = keyring.get_keyring()
                if not isinstance(kr, fail.Keyring) and kr.priority > 0:
                    self._keyring = keyring
            except Exception:  # pragma: no cover - platform dependent
                self._keyring = None
        self._listeners: list = []

    # -- public API -----------------------------------------------------------------
    @property
    def backend(self) -> str:
        return "keyring" if self._keyring is not None else "file"

    def get(self, name: str) -> str | None:
        value, _ = self._resolve(name)
        return value

    def status(self, name: str) -> SecretStatus:
        value, source = self._resolve(name)
        return SecretStatus(name=name, configured=bool(value), source=source)

    def all_status(self) -> dict[str, dict]:
        out = {}
        for n in ALL_SECRETS:
            st = self.status(n)
            out[n] = {"configured": st.configured, "source": st.source}
        return out

    def set(self, name: str, value: str | None) -> None:
        if name not in ENV_VARS:
            raise KeyError(name)
        value = (value or "").strip()
        if any(ord(c) < 32 for c in value):
            raise ValueError("secret contains control characters")
        if self._keyring is not None:
            if value:
                self._keyring.set_password(KEYRING_SERVICE, name, value)
            else:
                try:
                    self._keyring.delete_password(KEYRING_SERVICE, name)
                except Exception:
                    pass
        else:
            with self._lock:
                data = self._read_file()
                if value:
                    data[name] = value
                else:
                    data.pop(name, None)
                self._write_file(data)
        for cb in list(self._listeners):
            try:
                cb(name)
            except Exception:  # pragma: no cover
                log.exception("secret listener failed")

    def on_change(self, cb) -> None:
        self._listeners.append(cb)

    def known_values(self) -> list[str]:
        """All configured secret values (for log redaction)."""
        vals = []
        for n in ALL_SECRETS:
            v = self.get(n)
            if v and len(v) >= 4:
                vals.append(v)
        return vals

    # -- internals --------------------------------------------------------------------
    def _resolve(self, name: str) -> tuple[str | None, str]:
        if name not in ENV_VARS:
            raise KeyError(name)
        for env in ENV_VARS[name]:
            v = os.environ.get(env)
            if v and v.strip():
                return v.strip(), "env"
        if self._keyring is not None:
            try:
                v = self._keyring.get_password(KEYRING_SERVICE, name)
                if v:
                    return v, "keyring"
            except Exception:  # pragma: no cover
                log.warning("keyring read failed for %s", name)
        with self._lock:
            v = self._read_file().get(name)
        if v:
            return v, "file"
        return None, "none"

    def _read_file(self) -> dict:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.error("secrets file is unreadable; ignoring it")
            return {}

    def _write_file(self, data: dict) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - Windows ACLs
            pass
        os.replace(tmp, self._file)


def load_or_create_admin_token(data_dir: Path) -> str:
    """Local dashboard access token, created on first run (owner-only file)."""
    path = data_dir / "admin_token"
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    except FileNotFoundError:
        pass
    data_dir.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    path.write_text(tok, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover
        pass
    return tok
