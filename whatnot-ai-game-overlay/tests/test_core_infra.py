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
    assert s.overlay.size == "balanced" and s.overlay.position == "top-center"
    assert s.overlay.show_attribution is False and s.overlay.show_retrieval_time is False
    assert s.overlay.idle_panel is True
    assert s.pricing.price_refresh_s == 3600
    assert s.pricing.min_request_interval_s >= 1.0
    assert s.recognition.model == "claude-opus-5-5"


def test_settings_migration_turns_off_retrieval_time():
    from gameoverlay.config import SETTINGS_VERSION, migrate_settings

    old = RuntimeSettings().model_dump()
    old.pop("settings_version")
    old["overlay"]["show_retrieval_time"] = True  # saved by an older version
    m = migrate_settings(old)
    assert m["overlay"]["show_retrieval_time"] is False and m["settings_version"] == SETTINGS_VERSION
    assert RuntimeSettings.model_validate(m).overlay.position == old["overlay"]["position"]
    # current-version settings keep the operator's own choice
    m["overlay"]["show_retrieval_time"] = True
    assert migrate_settings(m)["overlay"]["show_retrieval_time"] is True


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


def test_existing_install_settings_are_migrated_on_start(tmp_path):
    """An install that saved settings before v2 stops showing the time after upgrading."""
    from gameoverlay.config import AppConfig, Environment
    from gameoverlay.services import Services

    cfg = AppConfig(env=Environment.TEST, data_dir=tmp_path, port=8765)
    old = RuntimeSettings().merged({"overlay": {"show_retrieval_time": True, "size": "large"}}).model_dump()
    old.pop("settings_version")
    d = Database(cfg.db_path)
    d.set_setting("runtime", old)
    d.close()
    svc = Services(cfg)
    try:
        assert svc.settings.overlay.show_retrieval_time is False
        assert svc.settings.overlay.size == "large", "other operator choices are preserved"
        assert svc.db.get_setting("runtime")["settings_version"] == 2
    finally:
        svc.db.close()
