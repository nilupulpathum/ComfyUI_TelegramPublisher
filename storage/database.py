"""SQLite database bootstrap (T035, T040).

Owns the ``publish_jobs`` + ``generations`` schema per TECHNICAL_SPEC
section 7, plus a ``schema_version`` table recording the applied schema
version (currently 2) for migration/version behavior.

Design decision: this module does NOT create ``accounts``/``destinations``
tables. Those are owned by the T007 JSON config (``storage/config.py``):
secrets must stay out of SQLite and workflow JSON (FR-001), and the
follow-up node layer references accounts/destinations by ID only. SQLite
stores publish history keyed by ``destination_id`` strings.

Job lifecycle (T040 queue lifecycle; T042 retry scheduling refines it):

- ``"pending"``: created by the synchronous publish path. Kept valid;
  existing rows are never rewritten by the v1->v2 migration.
- ``"queued"``: persisted by ``PublishQueue.enqueue`` and awaiting the
  worker. ``next_retry_at`` is NULL for immediately-due jobs, or a UTC
  ISO timestamp for scheduled retries (``queued`` + non-NULL
  ``next_retry_at`` in the future means "not due yet").
- ``"sending"``: claimed by the worker (row updated before the upload
  attempt; ``attempts`` is incremented and persisted per attempt).
- ``"success"``: terminal; ``telegram_message_id`` holds the sender's
  return value.
- ``"failed"``: terminal; ``error_code`` is the exception type name
  (``"PayloadLost"`` when the in-memory bytes are gone after a restart,
  ``"RetryExhausted"`` when attempts reach ``max_attempts``) and
  ``error_message`` is ``str(exc)`` (token-free by construction: the
  sender callable owns secrets and typed errors redact them).

Transitions: ``pending``/``queued`` -> ``sending`` -> ``success``/``failed``.
Transient failures requeue as ``sending`` -> ``queued`` with a new
``next_retry_at`` (computed from ``RetryPolicy.delay_for`` or a +30s
default); T042 owns scheduling refinement (backoff tuning, retry_after
honoring, max-attempts policy). ``failed`` is terminal and the local
record is never deleted silently.

Schema versions:

- v1: ``publish_jobs`` without ``next_retry_at``; statuses
  ``"pending"``/``"success"``/``"failed"``.
- v2 (T040): adds ``publish_jobs.next_retry_at TEXT`` (nullable) plus
  the ``"queued"``/``"sending"`` lifecycle. Migration is a single
  ``ALTER TABLE ... ADD COLUMN`` which preserves all existing rows and
  leaves their statuses untouched.

Conventions:
- ids are uuid4 hex strings (callers may supply their own id);
- timestamps are UTC ISO strings (``datetime.now(timezone.utc).isoformat()``);
- job status values are plain strings (see lifecycle above; repositories
  do not hard-validate status values for that reason).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2


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
        """Create tables + migrate to SCHEMA_VERSION (idempotent).

        Fresh databases get the v2 schema directly. Databases built with
        the v1 schema (``publish_jobs`` without ``next_retry_at``) are
        upgraded with ``ALTER TABLE ... ADD COLUMN`` so existing rows and
        their statuses are preserved. The ``schema_version`` table ends
        with exactly one row holding :data:`SCHEMA_VERSION`.
        """
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
                    next_retry_at TEXT,
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
            self._ensure_v2_column(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_publish_jobs_status_retry
                ON publish_jobs (status, next_retry_at, created_at)
                """
            )
            # Keep exactly one version row: drop stale versions, then record
            # the current one. Preserves publish_jobs/generations rows.
            conn.execute(
                "DELETE FROM schema_version WHERE version != ?",
                (SCHEMA_VERSION,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at)"
                " VALUES (?, ?)",
                (SCHEMA_VERSION, utcnow_iso()),
            )

    @staticmethod
    def _ensure_v2_column(conn: sqlite3.Connection) -> None:
        """Add ``publish_jobs.next_retry_at`` when missing (v1 -> v2)."""
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(publish_jobs)").fetchall()
        }
        if "next_retry_at" not in cols:
            conn.execute("ALTER TABLE publish_jobs ADD COLUMN next_retry_at TEXT")
