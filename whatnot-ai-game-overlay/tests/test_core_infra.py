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
    assert s.overlay.position == "top-center" and s.overlay.edge_offset == 0.0
    assert s.overlay.round_whole_dollars is True
    assert s.overlay.show_prices is True, "prices are shown on the stream unless the operator hides them"
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


def test_settings_migration_moves_strip_to_its_designed_place():
    from gameoverlay.config import migrate_settings

    v2 = RuntimeSettings().model_dump()
    v2["settings_version"] = 2
    v2["overlay"].update(edge_offset=0.008, position="side-left")
    m = migrate_settings(v2)
    assert m["overlay"]["edge_offset"] == 0.0 and m["overlay"]["position"] == "top-center"
    v2["overlay"].update(edge_offset=0.05, position="bottom-right")
    m = migrate_settings(v2)
    assert m["overlay"]["edge_offset"] == 0.05, "an operator's own offset is kept"
    assert m["overlay"]["position"] == "bottom-center"
    v1 = RuntimeSettings().model_dump()
    v1.pop("settings_version")
    v1["overlay"]["edge_offset"] = 0.035
    assert migrate_settings(v1)["overlay"]["edge_offset"] == 0.0


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
        from gameoverlay.config import SETTINGS_VERSION

        assert svc.db.get_setting("runtime")["settings_version"] == SETTINGS_VERSION
    finally:
        svc.db.close()


async def test_clean_shutdown_recorded_even_if_teardown_hangs(tmp_path):
    """Windows gives a closing console only seconds: the clean-exit mark must come first."""
    import asyncio

    from gameoverlay.config import AppConfig, Environment
    from gameoverlay.services import Services

    cfg = AppConfig(env=Environment.TEST, data_dir=tmp_path, port=8765)
    svc = Services(cfg)

    async def hang():
        await asyncio.sleep(3600)

    svc.engine.stop = hang
    task = asyncio.create_task(svc.stop())
    await asyncio.sleep(0.2)
    row = svc.db.query_one("SELECT clean_shutdown FROM sessions WHERE id=?", (svc.session_id,))
    assert row["clean_shutdown"] == 1, "marked clean before the slow teardown steps"
    task.cancel()
    try:
        await task
    except BaseException:
        pass
