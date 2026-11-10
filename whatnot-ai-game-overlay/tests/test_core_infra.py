"""Database migrations, settings persistence, secrets, config safety, log redaction."""

from __future__ import annotations

import logging

import pytest

from gameoverlay.config import AppConfig, CropRegion, RuntimeSettings
from gameoverlay.db import LATEST_VERSION, Database
from gameoverlay.logging_setup import RedactingFilter
from gameoverlay.secrets_store import SecretStore, load_or_create_admin_token


def test_migrations_apply_and_are_idempotent(tmp_path):
    d = Database(tmp_path / "m.sqlite3")
    assert d.schema_version() == LATEST_VERSION
    d.close()
    d2 = Database(tmp_path / "m.sqlite3")
    assert d2.migrate() == LATEST_VERSION
    d2.close()


def test_newer_schema_refused(tmp_path):
    d = Database(tmp_path / "n.sqlite3")
    d.execute("INSERT INTO schema_migrations(version, description, applied_at) VALUES (999,'x',0)")
    d.close()
    with pytest.raises(RuntimeError, match="newer"):
        Database(tmp_path / "n.sqlite3")


def test_crash_detection(tmp_path):
    d = Database(tmp_path / "c.sqlite3")
    assert d.start_session("a") is False
    assert d.start_session("b") is True  # "a" never shut down cleanly
    d.end_session("b")
    assert d.start_session("c") is False
    d.close()


def test_settings_roundtrip(db):
    s = RuntimeSettings().merged({"overlay": {"size": "large"}, "detection": {"sample_interval_ms": 500}})
    db.set_setting("runtime", s.model_dump())
    loaded = RuntimeSettings.model_validate(db.get_setting("runtime"))
    assert loaded.overlay.size == "large" and loaded.detection.sample_interval_ms == 500


def test_invalid_settings_rejected():
    with pytest.raises(Exception):
        RuntimeSettings().merged({"pricing": {"min_request_interval_s": 0.5}})
    with pytest.raises(Exception):
        RuntimeSettings().merged({"overlay": {"position": "middle"}})
    with pytest.raises(Exception):
        CropRegion(x=0.8, y=0, w=0.5, h=1)


def test_defaults_match_design_brief():
    s = RuntimeSettings()
    assert s.overlay.size == "balanced" and s.overlay.position == "bottom-center"
    assert s.pricing.price_refresh_s == 3600
    assert s.pricing.min_request_interval_s >= 1.0
    assert s.recognition.model == "claude-opus-5-5"


def test_refuses_non_loopback_by_default(monkeypatch):
    with pytest.raises(Exception, match="non-loopback"):
        AppConfig(host="0.0.0.0")
    assert AppConfig(host="0.0.0.0", allow_non_loopback=True).host == "0.0.0.0"
    assert AppConfig().host == "127.0.0.1"


def test_secret_store_file_fallback(tmp_path, monkeypatch):
    s = SecretStore(tmp_path, use_keyring=False)
    assert s.status("pricecharting_token").configured is False
    s.set("pricecharting_token", "abc123secret")
    assert s.get("pricecharting_token") == "abc123secret"
    assert s.status("pricecharting_token").source == "file"
    monkeypatch.setenv("PRICECHARTING_TOKEN", "fromenv")
    assert s.get("pricecharting_token") == "fromenv"
    s.set("pricecharting_token", "")
    monkeypatch.delenv("PRICECHARTING_TOKEN")
    assert s.get("pricecharting_token") is None
    assert "abc123secret" not in str(s.all_status())


def test_admin_token_persisted(tmp_path):
    a = load_or_create_admin_token(tmp_path)
    assert len(a) >= 32 and load_or_create_admin_token(tmp_path) == a


def test_redaction_filter():
    f = RedactingFilter(lambda: ["supersecretvalue123"])
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "key=%s url=%s ant=%s", (
        "supersecretvalue123",
        "https://www.pricecharting.com/api/product?t=abcdef0123456789&id=1",
        "sk-ant-api03-ABCDEFGHIJKLMNOP",
    ), None)
    f.filter(rec)
    out = rec.getMessage()
    assert "supersecretvalue123" not in out
    assert "abcdef0123456789" not in out
    assert "sk-ant-api03" not in out
