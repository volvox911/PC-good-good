"""SQLite persistence with forward-only, versioned migrations.

Separate caches are kept for:

* ``price_cache`` — PriceCharting prices per product ID (refreshed on a schedule).
* ``match_cache`` — recognized (title, platform, region, edition) -> product ID.
* ``search_cache`` — raw ``/api/products`` candidate lists per query.

plus ``history`` (what was shown), ``fingerprints`` (visual memory), ``api_usage``
(Claude spend), ``sessions`` and ``settings``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            ended_at REAL,
            clean_shutdown INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE price_cache (
            product_id TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            console_name TEXT NOT NULL,
            loose_cents INTEGER,
            cib_cents INTEGER,
            new_cents INTEGER,
            upc TEXT,
            release_date TEXT,
            retrieved_at REAL NOT NULL
        );
        CREATE TABLE match_cache (
            key TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            console_name TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('auto','manual')),
            score REAL,
            created_at REAL NOT NULL,
            last_used_at REAL NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE search_cache (
            query TEXT PRIMARY KEY,
            results_json TEXT NOT NULL,
            retrieved_at REAL NOT NULL
        );
        CREATE TABLE history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            console_name TEXT NOT NULL,
            loose_cents INTEGER,
            cib_cents INTEGER,
            new_cents INTEGER,
            price_retrieved_at REAL NOT NULL,
            freshness TEXT NOT NULL,
            shown_at REAL NOT NULL,
            source TEXT NOT NULL,
            recognized_title TEXT,
            recognized_platform TEXT,
            lookup_status TEXT NOT NULL
        );
        CREATE INDEX idx_history_shown ON history(shown_at DESC);
        CREATE TABLE fingerprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            hash_hex TEXT NOT NULL,
            hist_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX idx_fp_session ON fingerprints(session_id);
        CREATE TABLE api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX idx_usage_session ON api_usage(session_id);
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self.migrate()

    # -- migrations ---------------------------------------------------------------------
    def schema_version(self) -> int:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, description TEXT, applied_at REAL)"
            )
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            return int(row["v"] or 0)

    def migrate(self) -> int:
        current = self.schema_version()
        if current > LATEST_VERSION:
            raise RuntimeError(
                f"Database schema v{current} is newer than this app (v{LATEST_VERSION}). "
                "Update the application."
            )
        for version, desc, sql in MIGRATIONS:
            if version <= current:
                continue
            log.info("applying database migration %s: %s", version, desc)
            with self.transaction() as c:
                for stmt in _split_sql(sql):
                    c.execute(stmt)
                c.execute(
                    "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
                    (version, desc, time.time()),
                )
        return self.schema_version()

    # -- helpers ---------------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                pass

    # -- settings ----------------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value_json FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )

    # -- sessions ----------------------------------------------------------------------------
    def start_session(self, session_id: str) -> bool:
        """Record a new session. Returns True if the previous session crashed."""
        prev = self.query_one("SELECT id, clean_shutdown FROM sessions ORDER BY started_at DESC LIMIT 1")
        crashed = prev is not None and not prev["clean_shutdown"]
        self.execute("INSERT INTO sessions(id, started_at) VALUES (?,?)", (session_id, time.time()))
        return crashed

    def end_session(self, session_id: str) -> None:
        self.execute(
            "UPDATE sessions SET ended_at=?, clean_shutdown=1 WHERE id=?", (time.time(), session_id)
        )

    # -- maintenance -------------------------------------------------------------------------
    def prune(self, keep_history: int = 5000, keep_fingerprint_sessions: int = 3) -> None:
        self.execute(
            "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT ?)",
            (keep_history,),
        )
        self.execute(
            "DELETE FROM fingerprints WHERE session_id NOT IN "
            "(SELECT id FROM sessions ORDER BY started_at DESC LIMIT ?)",
            (keep_fingerprint_sessions,),
        )


def _split_sql(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
