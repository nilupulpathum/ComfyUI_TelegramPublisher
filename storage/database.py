"""SQLite database bootstrap (T035).

Owns the ``publish_jobs`` + ``generations`` schema per TECHNICAL_SPEC
section 7, plus a ``schema_version`` table recording the applied schema
version (currently 1) for migration/version behavior.

Design decision: this module does NOT create ``accounts``/``destinations``
tables. Those are owned by the T007 JSON config (``storage/config.py``):
secrets must stay out of SQLite and workflow JSON (FR-001), and the
follow-up node layer references accounts/destinations by ID only. SQLite
stores publish history keyed by ``destination_id`` strings.

Conventions:
- ids are uuid4 hex strings (callers may supply their own id);
- timestamps are UTC ISO strings (``datetime.now(timezone.utc).isoformat()``);
- job status values are plain strings: ``"pending"`` / ``"success"`` /
  ``"failed"`` (Epic 4 queue work may extend this set; repositories do
  not hard-validate status values for that reason).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """SQLite file bootstrap + connection factory."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a sqlite3 Connection (Row factory, foreign_keys=ON)."""
        conn = sqlite3.connect(str(self._path))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        """Create tables + record schema version (idempotent)."""
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS publish_jobs (
                    id TEXT PRIMARY KEY,
                    destination_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    image_hash TEXT,
                    filename TEXT,
                    caption TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    telegram_message_id TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY,
                    job_id TEXT REFERENCES publish_jobs(id) ON DELETE CASCADE,
                    prompt TEXT,
                    negative_prompt TEXT,
                    model TEXT,
                    seed TEXT,
                    steps TEXT,
                    cfg TEXT,
                    sampler TEXT,
                    scheduler TEXT,
                    width INTEGER,
                    height INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_publish_jobs_hash_dest
                ON publish_jobs (image_hash, destination_id)
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at)"
                " VALUES (?, ?)",
                (SCHEMA_VERSION, utcnow_iso()),
            )
