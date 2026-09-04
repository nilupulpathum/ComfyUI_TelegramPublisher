"""SQLite repositories for publish history (T035) + message IDs (T036).

T036 is satisfied by the ``telegram_message_id`` column on ``publish_jobs``
plus the repository round-trip below: the follow-up agent records the real
Telegram message id on the job (via :meth:`PublishJobRepository.update`)
after a successful send.

Status values are plain strings: ``"pending"`` / ``"success"`` / ``"failed"``
(Epic 4 queue work may extend this set; repositories require only a
non-empty status string and do not hard-validate against the known set).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from storage.database import Database, utcnow_iso


def _new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Publish jobs
# ---------------------------------------------------------------------------


@dataclass
class PublishJob:
    """One publish attempt. Only id/destination_id/status are required."""

    id: str | None = None
    destination_id: str = ""
    status: str = "pending"
    image_hash: str | None = None
    filename: str | None = None
    caption: str | None = None
    attempts: int = 0
    telegram_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


_JOB_COLUMNS = (
    "id",
    "destination_id",
    "status",
    "image_hash",
    "filename",
    "caption",
    "attempts",
    "telegram_message_id",
    "error_code",
    "error_message",
    "created_at",
    "updated_at",
)


def _job_from_row(row: sqlite3.Row) -> PublishJob:
    return PublishJob(**{col: row[col] for col in _JOB_COLUMNS})


class PublishJobRepository:
    """CRUD + history queries for ``publish_jobs``."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, job: PublishJob) -> PublishJob:
        """INSERT a job, filling id/created_at/updated_at when missing."""
        if not isinstance(job, PublishJob):
            raise ValueError("job must be a PublishJob instance")
        if not isinstance(job.destination_id, str) or not job.destination_id.strip():
            raise ValueError("job.destination_id must be a non-empty string")
        if not isinstance(job.status, str) or not job.status.strip():
            raise ValueError("job.status must be a non-empty string")
        now = utcnow_iso()
        if not job.id:
            job.id = _new_id()
        if not job.created_at:
            job.created_at = now
        if not job.updated_at:
            job.updated_at = job.created_at
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO publish_jobs"
                " (id, destination_id, status, image_hash, filename, caption,"
                " attempts, telegram_message_id, error_code, error_message,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.destination_id,
                    job.status,
                    job.image_hash,
                    job.filename,
                    job.caption,
                    job.attempts,
                    job.telegram_message_id,
                    job.error_code,
                    job.error_message,
                    job.created_at,
                    job.updated_at,
                ),
            )
        return job

    def get(self, job_id: str) -> PublishJob | None:
        """Return the job with this id, or None if unknown."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM publish_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def update(self, job: PublishJob) -> PublishJob:
        """UPDATE a job, refreshing updated_at.

        Raises:
            ValueError: If ``job`` has no id.
            KeyError: If no job with this id exists.
        """
        if not isinstance(job, PublishJob):
            raise ValueError("job must be a PublishJob instance")
        if not job.id:
            raise ValueError("job.id is required for update")
        job.updated_at = utcnow_iso()
        with self._db.connect() as conn:
            cursor = conn.execute(
                "UPDATE publish_jobs SET destination_id = ?, status = ?,"
                " image_hash = ?, filename = ?, caption = ?, attempts = ?,"
                " telegram_message_id = ?, error_code = ?, error_message = ?,"
                " created_at = ?, updated_at = ? WHERE id = ?",
                (
                    job.destination_id,
                    job.status,
                    job.image_hash,
                    job.filename,
                    job.caption,
                    job.attempts,
                    job.telegram_message_id,
                    job.error_code,
                    job.error_message,
                    job.created_at,
                    job.updated_at,
                    job.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown publish job id '{job.id}'")
        return job

    def find_by_hash(
        self,
        image_hash: str,
        destination_id: str,
        statuses: tuple[str, ...] = ("success",),
    ) -> PublishJob | None:
        """Newest job for this hash+destination filtered by status."""
        if not statuses:
            return None
        placeholders = ",".join("?" for _ in statuses)
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM publish_jobs"
                f" WHERE image_hash = ? AND destination_id = ?"
                f" AND status IN ({placeholders})"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (image_hash, destination_id, *statuses),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def list_recent(self, limit: int = 50) -> list[PublishJob]:
        """Newest jobs first.

        Raises:
            ValueError: If ``limit`` is not a positive int.
        """
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive int")
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM publish_jobs"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_job_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Generations
# ---------------------------------------------------------------------------


@dataclass
class Generation:
    """Generation parameters linked to a publish job via ``job_id``."""

    id: str | None = None
    job_id: str | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    model: str | None = None
    seed: str | None = None
    steps: str | None = None
    cfg: str | None = None
    sampler: str | None = None
    scheduler: str | None = None
    width: int | None = None
    height: int | None = None
    created_at: str | None = None


_GEN_COLUMNS = (
    "id",
    "job_id",
    "prompt",
    "negative_prompt",
    "model",
    "seed",
    "steps",
    "cfg",
    "sampler",
    "scheduler",
    "width",
    "height",
    "created_at",
)


def _gen_from_row(row: sqlite3.Row) -> Generation:
    return Generation(**{col: row[col] for col in _GEN_COLUMNS})


class GenerationRepository:
    """Persistence for ``generations`` rows linked to publish jobs."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, gen: Generation) -> Generation:
        """INSERT a generation, filling id/created_at when missing."""
        if not isinstance(gen, Generation):
            raise ValueError("gen must be a Generation instance")
        if not gen.id:
            gen.id = _new_id()
        if not gen.created_at:
            gen.created_at = utcnow_iso()
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO generations"
                " (id, job_id, prompt, negative_prompt, model, seed, steps,"
                " cfg, sampler, scheduler, width, height, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gen.id,
                    gen.job_id,
                    gen.prompt,
                    gen.negative_prompt,
                    gen.model,
                    gen.seed,
                    gen.steps,
                    gen.cfg,
                    gen.sampler,
                    gen.scheduler,
                    gen.width,
                    gen.height,
                    gen.created_at,
                ),
            )
        return gen

    def list_for_job(self, job_id: str) -> list[Generation]:
        """All generations for a job, oldest (insertion order) first."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM generations WHERE job_id = ? ORDER BY rowid ASC",
                (job_id,),
            ).fetchall()
        return [_gen_from_row(row) for row in rows]
