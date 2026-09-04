"""T035 + T036: SQLite history + message-id persistence. No network."""

import sqlite3

import pytest

from storage.database import SCHEMA_VERSION, Database
from storage.repositories import (
    Generation,
    GenerationRepository,
    PublishJob,
    PublishJobRepository,
)


def make_db(tmp_path, name="history.db"):
    db = Database(tmp_path / name)
    db.init_schema()
    return db


def make_repos(db):
    return PublishJobRepository(db), GenerationRepository(db)


# -- schema ----------------------------------------------------------------


def test_schema_creates_tables_and_version_row(tmp_path):
    db = make_db(tmp_path)
    with db.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"publish_jobs", "generations", "schema_version"} <= tables
        version = conn.execute(
            "SELECT version FROM schema_version WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
        assert version is not None
        assert version["version"] == SCHEMA_VERSION


def test_init_schema_is_idempotent(tmp_path):
    db = make_db(tmp_path)
    db.init_schema()  # second run must not fail or duplicate the version row
    with db.connect() as conn:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert [r["version"] for r in rows] == [SCHEMA_VERSION]


def test_publish_jobs_columns_match_spec(tmp_path):
    db = make_db(tmp_path)
    with db.connect() as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(publish_jobs)").fetchall()
        }
    assert cols == {
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
        "next_retry_at",
        "created_at",
        "updated_at",
    }


def test_connect_enables_foreign_keys(tmp_path):
    db = make_db(tmp_path)
    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_database_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "history.db"
    Database(nested).init_schema()
    assert nested.exists()


# -- job CRUD ---------------------------------------------------------------


def test_job_crud_round_trip(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    created = jobs.create(
        PublishJob(
            destination_id="chan-1",
            status="pending",
            image_hash="abc123",
            filename="out.png",
            caption="hello",
        )
    )
    assert created.id
    assert created.created_at and created.updated_at
    assert created.attempts == 0
    fetched = jobs.get(created.id)
    assert fetched is not None
    assert fetched.destination_id == "chan-1"
    assert fetched.status == "pending"
    assert fetched.image_hash == "abc123"
    assert fetched.filename == "out.png"
    assert fetched.caption == "hello"


def test_get_unknown_returns_none(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    assert jobs.get("no-such-id") is None


def test_create_supplied_id_is_kept(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    created = jobs.create(
        PublishJob(id="custom-id", destination_id="chan-1", status="pending")
    )
    assert created.id == "custom-id"
    assert jobs.get("custom-id") is not None


def test_update_refreshes_updated_at_and_persists(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    job = jobs.create(PublishJob(destination_id="chan-1", status="pending"))
    job.updated_at = "2000-01-01T00:00:00+00:00"  # force a stale stamp
    job.status = "success"
    job.attempts = 1
    updated = jobs.update(job)
    assert updated.updated_at != "2000-01-01T00:00:00+00:00"
    fetched = jobs.get(job.id)
    assert fetched.status == "success"
    assert fetched.attempts == 1
    assert fetched.updated_at == updated.updated_at


def test_update_missing_id_raises_value_error(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    with pytest.raises(ValueError):
        jobs.update(PublishJob(destination_id="chan-1", status="pending"))


def test_update_unknown_id_raises_key_error(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    ghost = PublishJob(id="ghost", destination_id="chan-1", status="failed")
    with pytest.raises(KeyError):
        jobs.update(ghost)


# -- find_by_hash ------------------------------------------------------------


def test_find_by_hash_scoped_to_destination(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    jobs.create(
        PublishJob(
            destination_id="chan-1", status="success", image_hash="hash-1"
        )
    )
    assert jobs.find_by_hash("hash-1", "chan-1") is not None
    assert jobs.find_by_hash("hash-1", "chan-2") is None
    assert jobs.find_by_hash("other-hash", "chan-1") is None


def test_find_by_hash_status_filter(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    jobs.create(
        PublishJob(destination_id="chan-1", status="failed", image_hash="hash-9")
    )
    assert jobs.find_by_hash("hash-9", "chan-1") is None  # default: success only
    assert (
        jobs.find_by_hash("hash-9", "chan-1", statuses=("failed",)) is not None
    )
    assert (
        jobs.find_by_hash("hash-9", "chan-1", statuses=("success", "failed"))
        is not None
    )


def test_find_by_hash_newest_first(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    old = jobs.create(
        PublishJob(
            destination_id="chan-1",
            status="success",
            image_hash="hash-7",
            created_at="2024-01-01T00:00:00+00:00",
            updated_at="2024-01-01T00:00:00+00:00",
        )
    )
    new = jobs.create(
        PublishJob(
            destination_id="chan-1",
            status="success",
            image_hash="hash-7",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
    )
    assert old.id != new.id
    hit = jobs.find_by_hash("hash-7", "chan-1")
    assert hit is not None
    assert hit.id == new.id


# -- list_recent --------------------------------------------------------------


def test_list_recent_order_and_limit(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    ids = []
    for day in ("01", "02", "03"):
        job = jobs.create(
            PublishJob(
                destination_id="chan-1",
                status="success",
                created_at=f"2025-06-{day}T00:00:00+00:00",
                updated_at=f"2025-06-{day}T00:00:00+00:00",
            )
        )
        ids.append(job.id)
    recent = jobs.list_recent(limit=2)
    assert [j.id for j in recent] == [ids[2], ids[1]]  # newest first
    assert len(jobs.list_recent()) == 3


def test_list_recent_rejects_bad_limit(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    for bad in (0, -1, -50):
        with pytest.raises(ValueError):
            jobs.list_recent(limit=bad)


# -- generations ---------------------------------------------------------------


def test_generation_link_and_list_for_job(tmp_path):
    db = make_db(tmp_path)
    jobs, gens = make_repos(db)
    job = jobs.create(PublishJob(destination_id="chan-1", status="success"))
    first = gens.create(
        Generation(job_id=job.id, prompt="a cat", model="m1", seed="7")
    )
    second = gens.create(
        Generation(job_id=job.id, prompt="a dog", width=512, height=512)
    )
    assert first.id and second.id
    assert first.created_at and second.created_at
    linked = gens.list_for_job(job.id)
    assert [g.id for g in linked] == [first.id, second.id]
    assert linked[0].prompt == "a cat"
    assert linked[1].width == 512
    assert gens.list_for_job("no-such-job") == []


# -- T036 message ids -----------------------------------------------------------


def test_telegram_message_id_round_trip(tmp_path):
    db = make_db(tmp_path)
    jobs, _ = make_repos(db)
    job = jobs.create(
        PublishJob(
            destination_id="chan-1",
            status="success",
            telegram_message_id="12345",
        )
    )
    assert jobs.get(job.id).telegram_message_id == "12345"
    job.telegram_message_id = "67890"  # follow-up agent records real ids here
    jobs.update(job)
    assert jobs.get(job.id).telegram_message_id == "67890"


# -- persistence -----------------------------------------------------------------


def test_history_persists_across_reopen(tmp_path):
    path = tmp_path / "history.db"
    db = Database(path)
    db.init_schema()
    jobs = PublishJobRepository(db)
    job = jobs.create(
        PublishJob(
            destination_id="chan-1",
            status="success",
            image_hash="persist-me",
            telegram_message_id="42",
        )
    )
    job_id = job.id

    reopened = Database(path)
    reopened.init_schema()
    fetched = PublishJobRepository(reopened).get(job_id)
    assert fetched is not None
    assert fetched.image_hash == "persist-me"
    assert fetched.telegram_message_id == "42"
    assert reopened.connect is not None
    # raw sqlite read proves it is really on disk, not in-memory
    raw = sqlite3.connect(str(path)).execute(
        "SELECT status FROM publish_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert raw[0] == "success"
