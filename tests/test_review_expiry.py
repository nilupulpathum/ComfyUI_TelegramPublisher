"""T071 tests: expiry of abandoned review payloads (TTL/GC).

``services.review.prune_expired`` unit coverage plus the poller-as-GC-host
integration. Fake clocks via the ``now`` parameter, fake bot clients, tmp
paths; no network, no real tokens (``TEST_TOKEN_xxx`` throughout).
"""

from __future__ import annotations

import logging
import socket
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from publisher_nodes.poll_commands import TelegramCommandPoller
from services.review import (
    DEFAULT_REVIEW_TTL_S,
    ReviewStore,
    prune_expired,
    review_dir_for,
)
from storage.config import (
    Account,
    BotSettings,
    ConfigStore,
    Destination,
    FileSecretStore,
)
from storage.database import Database
from storage.repositories import PublishJob, PublishJobRepository
from telegram.models import SendMessageResult, SendPhotoResult

FAKE_TOKEN = "TEST_TOKEN_xxx"
ACCOUNT_ID = "acc1"
DEST_ID = "dst1"
DEST_CHAT_ID = "-100999"
ADMIN_CHAT = "111"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

NOW = 1_720_000_000.0  # fixed fake clock (epoch seconds)
TTL = DEFAULT_REVIEW_TTL_S


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("tests must not make real network calls")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def make_jobs(tmp_path: Path) -> tuple[Database, PublishJobRepository, Path]:
    db_path = tmp_path / "history" / "publisher.sqlite3"
    db = Database(db_path)
    db.init_schema()
    review_dir = review_dir_for(db_path)
    return db, PublishJobRepository(db), review_dir


def seed(
    jobs: PublishJobRepository,
    review_dir: Path,
    job_id: str,
    *,
    status: str = "pending_review",
    created_at: Any = None,
    with_payload: bool = True,
) -> PublishJob:
    job = jobs.create(
        PublishJob(
            id=job_id,
            destination_id=DEST_ID,
            status=status,
            image_hash="a" * 64,
            filename="comfyui_8x8.png",
            caption="cap",
            attempts=0,
            created_at=created_at,
        )
    )
    if with_payload:
        ReviewStore(review_dir).save(job_id, PNG_BYTES)
    return job


def payload(review_dir: Path, job_id: str) -> Path:
    return review_dir / f"{job_id}.png"


# ---------------------------------------------------------------------------
# Defaults + boundary
# ---------------------------------------------------------------------------


def test_default_ttl_is_seven_days() -> None:
    assert DEFAULT_REVIEW_TTL_S == 7 * 24 * 3600


def test_expiry_boundary_old_failed_fresh_kept(tmp_path: Path) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "oldjob1", created_at=iso(NOW - TTL - 10))
    seed(jobs, review_dir, "freshjob1", created_at=iso(NOW - 60))

    counts = prune_expired(review_dir, jobs, now=NOW)

    assert counts == {"expired": 1, "orphans": 0, "kept": 1}
    old = jobs.get("oldjob1")
    assert old is not None and old.status == "failed"
    assert old.error_code == "Expired"
    assert old.error_message == "review expired without approval"
    assert not payload(review_dir, "oldjob1").exists()
    fresh = jobs.get("freshjob1")
    assert fresh is not None and fresh.status == "pending_review"
    assert fresh.error_code is None
    assert payload(review_dir, "freshjob1").is_file()


def test_future_timestamp_kept(tmp_path: Path) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "futurejob1", created_at=iso(NOW + 3600))

    assert prune_expired(review_dir, jobs, now=NOW) == {
        "expired": 0,
        "orphans": 0,
        "kept": 1,
    }
    assert payload(review_dir, "futurejob1").is_file()
    assert jobs.get("futurejob1") is not None
    assert jobs.get("futurejob1").status == "pending_review"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Orphans + missing rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["success", "failed"])
def test_terminal_row_orphan_swept_record_preserved(
    tmp_path: Path, status: str
) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "donejob1", status=status, created_at=iso(NOW - TTL - 99))

    counts = prune_expired(review_dir, jobs, now=NOW)

    assert counts == {"expired": 0, "orphans": 1, "kept": 0}
    assert not payload(review_dir, "donejob1").exists()
    row = jobs.get("donejob1")
    assert row is not None and row.status == status  # record preserved


def test_missing_row_file_swept(tmp_path: Path) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    ReviewStore(review_dir).save("ghostjob1", PNG_BYTES)

    counts = prune_expired(review_dir, jobs, now=NOW)

    assert counts == {"expired": 1, "orphans": 0, "kept": 0}
    assert not payload(review_dir, "ghostjob1").exists()


# ---------------------------------------------------------------------------
# Fail-safe preservation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["not-a-time", "2024-13-99", "   "])
def test_corrupt_timestamp_kept(tmp_path: Path, bad: str, caplog) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "badtsjob1", created_at=bad)

    with caplog.at_level(logging.WARNING):
        counts = prune_expired(review_dir, jobs, now=NOW)

    assert counts == {"expired": 0, "orphans": 0, "kept": 1}
    assert payload(review_dir, "badtsjob1").is_file()
    assert jobs.get("badtsjob1") is not None
    assert jobs.get("badtsjob1").status == "pending_review"  # type: ignore[union-attr]
    assert "unparseable created_at" in caplog.text


def test_unexpected_status_kept_with_warning(tmp_path: Path, caplog) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "queuedjob1", status="queued", created_at=iso(NOW - TTL - 10))

    with caplog.at_level(logging.WARNING):
        counts = prune_expired(review_dir, jobs, now=NOW)

    assert counts == {"expired": 0, "orphans": 0, "kept": 1}
    assert payload(review_dir, "queuedjob1").is_file()
    assert "unexpected status" in caplog.text


def test_invalid_stem_kept_with_warning_and_non_png_ignored(
    tmp_path: Path, caplog
) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "goodold1", created_at=iso(NOW - TTL - 10))
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "bad!chars.png").write_bytes(PNG_BYTES)  # stray operator file
    (review_dir / "notes.txt").write_text("operator notes", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        counts = prune_expired(review_dir, jobs, now=NOW)

    assert counts == {"expired": 1, "orphans": 0, "kept": 1}
    assert (review_dir / "bad!chars.png").is_file()  # stray preserved
    assert (review_dir / "notes.txt").is_file()  # non-png untouched, uncounted
    assert "not a review job id" in caplog.text


def test_missing_review_dir_yields_zeros(tmp_path: Path) -> None:
    _, jobs, _ = make_jobs(tmp_path)
    counts = prune_expired(tmp_path / "no-such-review-dir", jobs, now=NOW)
    assert counts == {"expired": 0, "orphans": 0, "kept": 0}


def test_bad_ttl_rejected(tmp_path: Path) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    with pytest.raises(ValueError, match="ttl_s"):
        prune_expired(review_dir, jobs, ttl_s=-1)
    with pytest.raises(ValueError, match="ttl_s"):
        prune_expired(review_dir, jobs, ttl_s="week")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Per-file error isolation (broken repo double)
# ---------------------------------------------------------------------------


class _FlakyJobs:
    """Repo double whose lookup fails for one id only."""

    def __init__(self, real: PublishJobRepository) -> None:
        self._real = real

    def get(self, job_id: str):
        if job_id == "badjob1":
            raise RuntimeError("db locked")
        return self._real.get(job_id)

    def update(self, job):
        return self._real.update(job)


def test_per_file_error_isolation(tmp_path: Path, caplog) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "badjob1", created_at=iso(NOW - TTL - 10))
    seed(jobs, review_dir, "goodold2", created_at=iso(NOW - TTL - 10))

    with caplog.at_level(logging.WARNING):
        counts = prune_expired(review_dir, _FlakyJobs(jobs), now=NOW)  # type: ignore[arg-type]

    assert counts == {"expired": 1, "orphans": 0, "kept": 1}
    assert payload(review_dir, "badjob1").is_file()  # failed row untouched
    assert jobs.get("badjob1") is not None
    assert jobs.get("badjob1").status == "pending_review"  # type: ignore[union-attr]
    assert not payload(review_dir, "goodold2").exists()  # sibling still swept
    assert "job lookup failed" in caplog.text


class _BoomUpdateJobs(_FlakyJobs):
    def update(self, job):
        if job.id == "boomjob1":
            raise RuntimeError("disk full")
        return self._real.update(job)


def test_update_failure_keeps_file_and_record(tmp_path: Path, caplog) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "boomjob1", created_at=iso(NOW - TTL - 10))

    with caplog.at_level(logging.WARNING):
        counts = prune_expired(review_dir, _BoomUpdateJobs(jobs), now=NOW)  # type: ignore[arg-type]

    assert counts == {"expired": 0, "orphans": 0, "kept": 1}
    assert payload(review_dir, "boomjob1").is_file()
    assert jobs.get("boomjob1") is not None
    assert jobs.get("boomjob1").status == "pending_review"  # type: ignore[union-attr]
    assert "job update failed" in caplog.text


# ---------------------------------------------------------------------------
# Exact combined counts
# ---------------------------------------------------------------------------


def test_combined_counts_exact(tmp_path: Path) -> None:
    _, jobs, review_dir = make_jobs(tmp_path)
    seed(jobs, review_dir, "old1", created_at=iso(NOW - TTL - 10))  # expired
    seed(jobs, review_dir, "fresh1", created_at=iso(NOW - 5))  # kept
    seed(jobs, review_dir, "done1", status="success", created_at=iso(NOW - TTL - 5))
    ReviewStore(review_dir).save("ghost1", PNG_BYTES)  # expired (no row)
    seed(jobs, review_dir, "badts1", created_at="garbage")  # kept

    assert prune_expired(review_dir, jobs, now=NOW) == {
        "expired": 2,
        "orphans": 1,
        "kept": 2,
    }


# ---------------------------------------------------------------------------
# Poller integration: GC host, warning-free run continues
# ---------------------------------------------------------------------------


class FakeBotClient:
    """Recording stand-in: empty polls, captured replies/photos."""

    def __init__(self, batches: list[list[dict]] | None = None) -> None:
        self._batches = [list(b) for b in (batches or [])]
        self.sent: list[tuple[str, str]] = []
        self.photos: list[dict] = []

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int | float = 30,
        *,
        client_timeout: float | None = None,
    ) -> list[dict]:
        batch = self._batches.pop(0) if self._batches else []
        if offset is None:
            return list(batch)
        return [u for u in batch if u.get("update_id", 0) >= offset]

    def send_message(
        self, chat_id: str, text: str, *, timeout: float | None = None
    ) -> SendMessageResult:
        self.sent.append((chat_id, text))
        return SendMessageResult(message_id=len(self.sent), chat_id=chat_id)

    def send_photo(
        self,
        chat_id: str,
        image_bytes: bytes,
        filename: str,
        caption: Any = None,
        **kwargs: Any,
    ) -> SendPhotoResult:
        self.photos.append({"chat_id": chat_id, "bytes": bytes(image_bytes)})
        return SendPhotoResult(message_id=900, chat_id=chat_id)


def test_poller_prunes_old_review_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id=ACCOUNT_ID, name="Main bot"))
    config.add_destination(
        Destination(
            id=DEST_ID, name="Channel", account_id=ACCOUNT_ID, chat_id=DEST_CHAT_ID
        )
    )
    secrets.set_token(ACCOUNT_ID, FAKE_TOKEN)
    config.save_settings(BotSettings(admin_chat_ids=[ADMIN_CHAT]))
    db_path = tmp_path / "history" / "publisher.sqlite3"
    db = Database(db_path)
    db.init_schema()
    repo = PublishJobRepository(db)
    review_dir = review_dir_for(db_path)
    repo.create(
        PublishJob(
            id="stalejob1",
            destination_id=DEST_ID,
            status="pending_review",
            filename="comfyui_8x8.png",
            caption="stale",
            attempts=0,
            created_at="2020-01-01T00:00:00+00:00",  # far older than any TTL
        )
    )
    ReviewStore(review_dir).save("stalejob1", PNG_BYTES)
    fake = FakeBotClient(batches=[[]])

    def factory(token: str) -> FakeBotClient:
        assert token == FAKE_TOKEN
        return fake

    node = TelegramCommandPoller(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=db_path,
    )
    with caplog.at_level(logging.INFO):
        (summary,) = node.poll(ACCOUNT_ID, 1, 1)

    assert summary == "no new commands"  # run continued normally
    assert not (review_dir / "stalejob1.png").exists()  # pruned
    row = PublishJobRepository(Database(db_path)).get("stalejob1")
    assert row is not None and row.status == "failed"
    assert row.error_code == "Expired"
    assert row.error_message == "review expired without approval"
    assert FAKE_TOKEN not in caplog.text
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert "telegram review expiry" in caplog.text
