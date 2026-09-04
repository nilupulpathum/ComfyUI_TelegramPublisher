"""T040/T041/T044: queue lifecycle, bounded worker, graceful shutdown.

No network, no tokens: sender callables are fakes only. Every threaded
test joins its worker (fixtures call stop(); final assert worker_alive
is False) so no threads leak.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from services.queue import PublishPayload, PublishQueue
from services.retry import RetryPolicy
from storage.database import SCHEMA_VERSION, Database
from storage.repositories import GenerationRepository, PublishJob, PublishJobRepository
from telegram.errors import (
    PermanentTelegramError,
    RateLimitError,
    TransientTelegramError,
)


def make_db(tmp_path, name="queue.db"):
    db = Database(tmp_path / name)
    db.init_schema()
    return db


def make_payload(**overrides):
    base = {
        "kind": "photo",
        "account_id": "acc-1",
        "destination_id": "dest-1",
        "chat_id": "chat-1",
        "files": ((b"bytes-1", "out.png"),),
        "caption": "hello",
        "metadata": {"filename": "out.png"},
    }
    base.update(overrides)
    return PublishPayload(**base)  # type: ignore[arg-type]


def make_queue(db, sender, **kwargs):
    kwargs.setdefault("poll_interval", 0.02)
    return PublishQueue(db, sender, **kwargs)


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def repo(db):
    return PublishJobRepository(db)


# -- enqueue ---------------------------------------------------------------


def test_enqueue_persists_row_before_buffering(tmp_path):
    db = make_db(tmp_path)
    q = make_queue(db, lambda p: "m1")
    try:
        payload = make_payload()
        job_id = q.enqueue(payload)
        assert payload.job_id == job_id
        # Kill the in-memory buffer: the row must still exist.
        with q._lock:
            q._buffer.clear()
        assert q.depth == 0
        row = repo(db).get(job_id)
        assert row is not None
        assert row.status == "queued"
        assert row.attempts == 0
        assert row.filename == "out.png"
        assert row.caption == "hello"
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_enqueue_works_before_start(tmp_path):
    db = make_db(tmp_path)
    q = make_queue(db, lambda p: "m1")
    try:
        assert q.worker_alive is False
        job_id = q.enqueue(make_payload())
        assert q.depth == 1
        assert q.pending_jobs() == 1
        assert repo(db).get(job_id).status == "queued"
        q.start()  # worker picks up persisted job
        assert wait_for(
            lambda: repo(db).get(job_id) is not None
            and repo(db).get(job_id).status == "success"
        )
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_full_rejection_loud_no_row(tmp_path):
    db = make_db(tmp_path)
    q = make_queue(db, lambda p: "m1", maxsize=1)
    try:
        q.enqueue(make_payload(files=((b"a", "a.png"),)))
        with pytest.raises(ValueError):
            q.enqueue(make_payload(files=((b"b", "b.png"),)))
        assert q.depth == 1
        assert len(repo(db).list_recent()) == 1
        assert repo(db).count_by_status().get("queued", 0) == 1
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_maxsize_invalid_rejected():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "q.db")
        db.init_schema()
        for bad in (0, -1, -100):
            with pytest.raises(ValueError):
                PublishQueue(db, lambda p: "x", maxsize=bad)


# -- happy path / failures --------------------------------------------------


def test_happy_path_success_row(tmp_path):
    db = make_db(tmp_path)
    q = make_queue(db, lambda p: "tg-123")
    try:
        q.start()
        job_id = q.enqueue(make_payload())
        assert wait_for(
            lambda: (row := repo(db).get(job_id)) is not None
            and row.status == "success"
        )
        row = repo(db).get(job_id)
        assert row.telegram_message_id == "tg-123"
        assert row.attempts == 1
        assert wait_for(lambda: q.depth == 0)
        assert q.pending_jobs() == 0
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_permanent_failure_failed_row(tmp_path):
    db = make_db(tmp_path)

    def sender(payload):
        raise PermanentTelegramError("bad request")

    q = make_queue(db, sender)
    try:
        q.start()
        job_id = q.enqueue(make_payload())
        assert wait_for(
            lambda: (row := repo(db).get(job_id)) is not None
            and row.status == "failed",
            timeout=5.0,
        )
        row = repo(db).get(job_id)
        assert row.error_code == "PermanentTelegramError"
        assert "bad request" in (row.error_message or "")
        assert row.attempts == 1
        assert wait_for(lambda: q.depth == 0)
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_transient_requeued_with_next_retry(tmp_path):
    db = make_db(tmp_path)

    def sender(payload):
        raise TransientTelegramError("timeout")

    q = make_queue(db, sender, retry_policy=None)  # default +30s delay
    try:
        q.start()
        job_id = q.enqueue(make_payload())
        assert wait_for(
            lambda: (row := repo(db).get(job_id)) is not None
            and row.status == "queued"
            and row.next_retry_at is not None,
            timeout=5.0,
        )
        row = repo(db).get(job_id)
        assert row.attempts == 1
        assert row.next_retry_at is not None
        # Not due for ~30s: worker idles, payload stays buffered.
        assert q.depth == 1
        assert repo(db).find_due(limit=10) == []
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_transient_rate_limit_requeues(tmp_path):
    db = make_db(tmp_path)

    def sender(payload):
        raise RateLimitError("slow down", retry_after=60)

    policy = RetryPolicy(
        sleep=lambda s: None, base_delay=1.0, max_delay=30.0, jitter=0.0
    )
    q = make_queue(db, sender, retry_policy=policy)
    try:
        q.start()
        job_id = q.enqueue(make_payload())
        assert wait_for(
            lambda: (row := repo(db).get(job_id)) is not None
            and row.next_retry_at is not None,
            timeout=5.0,
        )
        row = repo(db).get(job_id)
        assert row.status == "queued"
        assert row.error_code == "RateLimitError"
        assert row.attempts == 1
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_exhaustion_failed_retry_exhausted(tmp_path):
    db = make_db(tmp_path)

    def sender(payload):
        raise TransientTelegramError("still down")

    q = make_queue(db, sender, max_attempts=1)
    try:
        q.start()
        job_id = q.enqueue(make_payload())
        assert wait_for(
            lambda: (row := repo(db).get(job_id)) is not None
            and row.status == "failed",
            timeout=5.0,
        )
        row = repo(db).get(job_id)
        assert row.error_code == "RetryExhausted"
        assert row.attempts == 1
        assert wait_for(lambda: q.depth == 0)
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_missing_payload_failed_payload_lost(tmp_path):
    db = make_db(tmp_path)
    q = make_queue(db, lambda p: "never-called")
    try:
        job_id = q.enqueue(make_payload())
        # Simulate process restart: bytes gone, row survives.
        with q._lock:
            q._buffer.clear()
        q.start()
        assert wait_for(
            lambda: (row := repo(db).get(job_id)) is not None
            and row.status == "failed",
            timeout=5.0,
        )
        row = repo(db).get(job_id)
        assert row.error_code == "PayloadLost"
        assert row.error_message
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


# -- lifecycle / shutdown ----------------------------------------------------


def test_stop_joins_in_flight(tmp_path):
    db = make_db(tmp_path)
    entered = threading.Event()

    def sender(payload):
        entered.set()
        time.sleep(0.5)  # slow but finite: stop() must wait for it
        return "late-msg"

    q = make_queue(db, sender)
    try:
        q.start()
        job_id = q.enqueue(make_payload())
        assert entered.wait(timeout=5.0)
        assert q.worker_alive is True
        assert q.stop(timeout=5.0) is True  # join waits for sender
        assert q.worker_alive is False
        row = repo(db).get(job_id)
        assert row.status == "success"
        assert row.telegram_message_id == "late-msg"
    finally:
        q.stop(timeout=5.0)
        assert q.worker_alive is False


def test_stop_timeout_false_path(tmp_path):
    db = make_db(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def sender(payload):
        entered.set()
        assert release.wait(timeout=10.0)
        return "eventual"

    q = make_queue(db, sender)
    try:
        q.start()
        q.enqueue(make_payload())
        assert entered.wait(timeout=5.0)
        assert q.stop(timeout=0.1) is False  # sender still blocked
        assert q.worker_alive is True
        release.set()
        assert q.stop(timeout=5.0) is True
        assert q.worker_alive is False
    finally:
        release.set()
        q.stop(timeout=5.0)
        assert q.worker_alive is False


def test_no_restart_after_stop(tmp_path):
    db = make_db(tmp_path)
    q = make_queue(db, lambda p: "x")
    try:
        q.start()
        assert q.stop() is True
        with pytest.raises(ValueError, match="shut down"):
            q.start()
        with pytest.raises(ValueError, match="shut down"):
            q.enqueue(make_payload())
    finally:
        assert q.worker_alive is False


def test_start_idempotent(tmp_path):
    db = make_db(tmp_path)
    q = make_queue(db, lambda p: "x")
    try:
        q.start()
        first = q._thread
        q.start()
        assert q._thread is first
        assert q.worker_alive is True
    finally:
        assert q.stop() is True
        assert q.worker_alive is False


def test_context_manager_stops(tmp_path):
    db = make_db(tmp_path)
    with make_queue(db, lambda p: "x") as q:
        assert q.worker_alive is True
        q.enqueue(make_payload())
    assert q.worker_alive is False


# -- storage: migration / find_due / count ------------------------------------


def test_migration_v1_to_v2_preserves_rows(tmp_path):
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(str(path))
    try:
        raw.execute(
            """
            CREATE TABLE publish_jobs (
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
        raw.execute(
            """
            CREATE TABLE generations (
                id TEXT PRIMARY KEY,
                job_id TEXT REFERENCES publish_jobs(id) ON DELETE CASCADE,
                prompt TEXT, negative_prompt TEXT, model TEXT, seed TEXT,
                steps TEXT, cfg TEXT, sampler TEXT, scheduler TEXT,
                width INTEGER, height INTEGER, created_at TEXT NOT NULL
            )
            """
        )
        raw.execute(
            "CREATE TABLE schema_version"
            " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        raw.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (1, ?)",
            ("2024-01-01T00:00:00+00:00",),
        )
        raw.execute(
            "INSERT INTO publish_jobs (id, destination_id, status,"
            " image_hash, filename, caption, attempts, telegram_message_id,"
            " error_code, error_message, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-v1",
                "dest-1",
                "pending",
                "h1",
                "a.png",
                "cap",
                2,
                None,
                None,
                None,
                "2024-05-01T00:00:00+00:00",
                "2024-05-01T00:00:00+00:00",
            ),
        )
        raw.execute(
            "INSERT INTO publish_jobs (id, destination_id, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "job-v1-ok",
                "dest-1",
                "success",
                "2024-05-02T00:00:00+00:00",
                "2024-05-02T00:00:00+00:00",
            ),
        )
        raw.commit()
    finally:
        raw.close()

    db = Database(path)
    db.init_schema()  # must ALTER, preserving rows and statuses

    with db.connect() as conn:
        cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(publish_jobs)")
        }
        assert "next_retry_at" in cols
        versions = [
            r["version"] for r in conn.execute("SELECT version FROM schema_version")
        ]
        assert versions == [SCHEMA_VERSION]
        assert SCHEMA_VERSION == 2

    jobs = PublishJobRepository(db)
    kept = jobs.get("job-v1")
    assert kept is not None
    assert kept.status == "pending"  # never rewritten
    assert kept.image_hash == "h1"
    assert kept.attempts == 2
    assert kept.next_retry_at is None
    assert jobs.get("job-v1-ok").status == "success"
    # Second init stays idempotent.
    db.init_schema()
    assert jobs.get("job-v1").status == "pending"


def test_find_due_ordering_and_filtering(tmp_path):
    db = make_db(tmp_path)
    jobs = PublishJobRepository(db)
    old = jobs.create(
        PublishJob(
            destination_id="d",
            status="queued",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
    )
    new = jobs.create(
        PublishJob(
            destination_id="d",
            status="queued",
            created_at="2025-02-01T00:00:00+00:00",
            updated_at="2025-02-01T00:00:00+00:00",
        )
    )
    future = jobs.create(
        PublishJob(
            destination_id="d",
            status="queued",
            next_retry_at="2999-01-01T00:00:00+00:00",
            created_at="2024-01-01T00:00:00+00:00",
            updated_at="2024-01-01T00:00:00+00:00",
        )
    )
    sending = jobs.create(
        PublishJob(destination_id="d", status="sending")
    )
    due_now = jobs.create(
        PublishJob(
            destination_id="d",
            status="queued",
            next_retry_at="2025-01-15T00:00:00+00:00",
            created_at="2025-01-10T00:00:00+00:00",
            updated_at="2025-01-10T00:00:00+00:00",
        )
    )
    due = jobs.find_due(limit=10, now="2025-03-01T00:00:00+00:00")
    assert [j.id for j in due] == [old.id, due_now.id, new.id]
    assert future.id not in {j.id for j in due}
    assert sending.id not in {j.id for j in due}
    assert [j.id for j in jobs.find_due(limit=1, now="2025-03-01T00:00:00+00:00")] == [
        old.id
    ]
    assert jobs.find_due(limit=10, now="2020-01-01T00:00:00+00:00") == [
        j for j in due if j.next_retry_at is None
    ]
    for bad in (0, -1):
        with pytest.raises(ValueError):
            jobs.find_due(limit=bad)


def test_count_by_status(tmp_path):
    db = make_db(tmp_path)
    jobs = PublishJobRepository(db)
    assert jobs.count_by_status() == {}
    jobs.create(PublishJob(destination_id="d", status="queued"))
    jobs.create(PublishJob(destination_id="d", status="queued"))
    jobs.create(PublishJob(destination_id="d", status="success"))
    counts = jobs.count_by_status()
    assert counts == {"queued": 2, "success": 1}


def test_worker_success_writes_generation_row(tmp_path):
    db = make_db(tmp_path)
    q = make_queue(db, lambda p: "m42")
    try:
        q.start()
        payload = make_payload(
            metadata={
                "filename": "out.png",
                "prompt": "a cat",
                "seed": "7",
                "width": "512",
                "height": "512",
            }
        )
        job_id = q.enqueue(payload)
        assert wait_for(
            lambda: (row := repo(db).get(job_id)) is not None
            and row.status == "success"
        )
        gens = GenerationRepository(db).list_for_job(job_id)
        assert len(gens) == 1
        assert gens[0].prompt == "a cat"
        assert gens[0].seed == "7"
        assert gens[0].width == 512
        assert gens[0].height == 512
    finally:
        q.stop()
        assert not q.worker_alive
