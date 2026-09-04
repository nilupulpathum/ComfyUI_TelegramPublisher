"""T042/T043 + Epic 4 wiring: retry scheduling, queue status, background publish.

Fake senders/transports only. No real network (urllib/socket blocked),
no real tokens (``TEST_TOKEN_xxx`` throughout), no ComfyUI import.
Threaded tests always stop their worker.
"""

from __future__ import annotations

import socket
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import publisher_nodes
import publisher_nodes.send_album as send_album_mod
import publisher_nodes.send_image as send_image_mod
from publisher_nodes.send_album import TelegramSendAlbum
from publisher_nodes.send_image import TelegramSendImage
from services.dedup import sha256_hex
from services.encoder import encode_image
from services.queue import (
    PublishPayload,
    PublishQueue,
    QueueStatus,
    _seconds_until_due,
    _shutdown_shared_queues,
    get_shared_queue,
)
from services.retry import RetryPolicy
from storage.config import Account, ConfigStore, Destination, FileSecretStore
from storage.database import Database
from storage.repositories import PublishJob, PublishJobRepository
from telegram.errors import RateLimitError, TransientTelegramError

FAKE_TOKEN = "TEST_TOKEN_xxx"
ACCOUNT_ID = "acc1"
DEST_ID = "dst1"
CHAT_ID = "-100123"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("tests must not make real network calls")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


def make_db(tmp_path: Path, name: str = "bg.db") -> Database:
    db = Database(tmp_path / name)
    db.init_schema()
    return db


def make_payload(**overrides: Any) -> PublishPayload:
    base: dict[str, Any] = {
        "kind": "photo",
        "account_id": ACCOUNT_ID,
        "destination_id": DEST_ID,
        "chat_id": CHAT_ID,
        "files": ((b"bytes-1", "out.png"),),
        "caption": "hello",
        "metadata": {"filename": "out.png"},
    }
    base.update(overrides)
    return PublishPayload(**base)


def make_stores(tmp_path: Path) -> tuple[ConfigStore, FileSecretStore]:
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id=ACCOUNT_ID, name="Main bot"))
    config.add_destination(
        Destination(
            id=DEST_ID, name="Channel", account_id=ACCOUNT_ID, chat_id=CHAT_ID
        )
    )
    secrets.set_token(ACCOUNT_ID, FAKE_TOKEN)
    return config, secrets


def frame(seed: int = 7, h: int = 8, w: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random((h, w, 3)).astype(np.float32)


class FakeQueue:
    """Injectable stand-in for PublishQueue: records payloads, no threads."""

    def __init__(self, job_id: str = "job-1") -> None:
        self.payloads: list[PublishPayload] = []
        self._job_id = job_id

    def enqueue(self, payload: PublishPayload) -> str:
        self.payloads.append(payload)
        return self._job_id


def boom_client_factory(token: str):
    raise AssertionError("background path must not touch the network")


# ---------------------------------------------------------------------------
# T042: server-directed retry_after honored precisely
# ---------------------------------------------------------------------------


def test_retry_policy_delay_for_honors_retry_after_capped(tmp_path: Path):
    policy = RetryPolicy(sleep=lambda s: None, max_delay=30.0, jitter=0.0)
    assert policy.delay_for(1, RateLimitError("slow", retry_after=120)) == 30.0
    assert policy.delay_for(1, RateLimitError("slow", retry_after=5)) == 5.0
    assert policy.delay_for(1, RateLimitError("slow")) is not None


def test_rate_limit_next_retry_at_equals_now_plus_capped_retry_after(
    tmp_path: Path,
):
    db = make_db(tmp_path)
    policy = RetryPolicy(
        sleep=lambda s: None, base_delay=1.0, max_delay=30.0, jitter=0.0
    )

    def sender(payload: PublishPayload) -> str:
        raise RateLimitError("slow down", retry_after=120)

    q = PublishQueue(db, sender, retry_policy=policy)
    try:
        job_id = q.enqueue(make_payload())
        before = datetime.now(timezone.utc)
        q._process_one(job_id)  # synchronous: no worker timing flakiness
        row = PublishJobRepository(db).get(job_id)
        assert row is not None
        assert row.status == "queued"
        assert row.attempts == 1
        assert row.next_retry_at is not None
        due = datetime.fromisoformat(row.next_retry_at)
        skew = (due - before).total_seconds()
        # min(retry_after=120, max_delay=30) == 30s, within clock tolerance.
        assert 25.0 <= skew <= 35.0
    finally:
        assert q.stop() is True


def test_small_retry_after_used_verbatim(tmp_path: Path):
    db = make_db(tmp_path)
    policy = RetryPolicy(
        sleep=lambda s: None, base_delay=1.0, max_delay=30.0, jitter=0.0
    )

    def sender(payload: PublishPayload) -> str:
        raise RateLimitError("slow down", retry_after=5)

    q = PublishQueue(db, sender, retry_policy=policy)
    try:
        job_id = q.enqueue(make_payload())
        before = datetime.now(timezone.utc)
        q._process_one(job_id)
        row = PublishJobRepository(db).get(job_id)
        assert row is not None and row.next_retry_at is not None
        skew = (datetime.fromisoformat(row.next_retry_at) - before).total_seconds()
        assert 0.0 <= skew <= 10.0
    finally:
        assert q.stop() is True


def test_no_policy_default_plus_30s(tmp_path: Path):
    db = make_db(tmp_path)

    def sender(payload: PublishPayload) -> str:
        raise TransientTelegramError("timeout")

    q = PublishQueue(db, sender, retry_policy=None)
    try:
        job_id = q.enqueue(make_payload())
        before = datetime.now(timezone.utc)
        q._process_one(job_id)
        row = PublishJobRepository(db).get(job_id)
        assert row is not None and row.next_retry_at is not None
        skew = (datetime.fromisoformat(row.next_retry_at) - before).total_seconds()
        assert 25.0 <= skew <= 35.0
    finally:
        assert q.stop() is True


# ---------------------------------------------------------------------------
# T042: due-wakeup -- no early attempt, no spin
# ---------------------------------------------------------------------------


def test_seconds_until_due_helpers():
    assert _seconds_until_due(None) == 0.0
    assert _seconds_until_due("") == 0.0
    assert _seconds_until_due("not-a-timestamp") == 0.0  # fail-open: due
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    assert _seconds_until_due(past) <= 0.0
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    assert 50.0 < _seconds_until_due(future) <= 60.0


def test_not_due_job_not_attempted_payload_kept(tmp_path: Path):
    db = make_db(tmp_path)
    calls: list[PublishPayload] = []

    def sender(payload: PublishPayload) -> str:
        calls.append(payload)
        return "never"

    q = PublishQueue(db, sender)
    try:
        job_id = q.enqueue(make_payload())
        # Simulate clock skew/race: row re-armed in the future after
        # find_due had already returned it.
        jobs = PublishJobRepository(db)
        job = jobs.get(job_id)
        assert job is not None
        job.next_retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat()
        jobs.update(job)

        wait_s = q._process_one(job_id)
        assert isinstance(wait_s, float) and wait_s > 0
        assert calls == []  # sender never attempted before due
        row = jobs.get(job_id)
        assert row is not None
        assert row.status == "queued"  # never claimed
        assert row.attempts == 0  # never attempted
        assert q.depth == 1  # payload put back / never popped
    finally:
        assert q.stop() is True


def test_due_job_proceeds_normally(tmp_path: Path):
    db = make_db(tmp_path)
    calls: list[PublishPayload] = []

    def sender(payload: PublishPayload) -> str:
        calls.append(payload)
        return "m-1"

    q = PublishQueue(db, sender)
    try:
        job_id = q.enqueue(make_payload())
        jobs = PublishJobRepository(db)
        job = jobs.get(job_id)
        assert job is not None
        job.next_retry_at = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat()
        jobs.update(job)
        assert q._process_one(job_id) is None
        assert len(calls) == 1
        assert jobs.get(job_id).status == "success"  # type: ignore[union-attr]
    finally:
        assert q.stop() is True


# ---------------------------------------------------------------------------
# T043: QueueStatus exact counts
# ---------------------------------------------------------------------------


def test_status_exact_counts_scripted(tmp_path: Path):
    db = make_db(tmp_path)
    jobs = PublishJobRepository(db)
    jobs.create(PublishJob(destination_id="d", status="queued"))
    jobs.create(PublishJob(destination_id="d", status="queued"))
    jobs.create(PublishJob(destination_id="d", status="sending"))
    jobs.create(PublishJob(destination_id="d", status="success"))
    jobs.create(PublishJob(destination_id="d", status="failed"))
    jobs.create(PublishJob(destination_id="d", status="failed"))

    q = PublishQueue(db, lambda p: "x", maxsize=7)
    try:
        q.enqueue(make_payload())  # one more queued row + one buffered payload
        status = q.status()
        assert isinstance(status, QueueStatus)
        assert status.queued == 3
        assert status.sending == 1
        assert status.success == 1
        assert status.failed == 2
        assert status.worker_running is False
        assert status.buffered == 1
        assert status.maxsize == 7
    finally:
        assert q.stop() is True


def test_status_worker_running_reflects_lifecycle(tmp_path: Path):
    db = make_db(tmp_path, "lifecycle.db")
    q = PublishQueue(db, lambda p: "x")
    try:
        assert q.status().worker_running is False
        q.start()
        assert q.status().worker_running is True
        assert q.status().buffered == 0
    finally:
        assert q.stop() is True
        assert q.status().worker_running is False


def test_status_empty_db_all_zero(tmp_path: Path):
    db = make_db(tmp_path, "empty.db")
    q = PublishQueue(db, lambda p: "x", maxsize=100)
    try:
        status = q.status()
        assert (status.queued, status.sending, status.success, status.failed) == (
            0, 0, 0, 0,
        )
        assert status.buffered == 0
        assert status.maxsize == 100
    finally:
        assert q.stop() is True


# ---------------------------------------------------------------------------
# Node False path: immediate return, payload contents, no network
# ---------------------------------------------------------------------------


def test_send_image_background_enqueues_and_returns_immediately(
    tmp_path: Path, caplog
):
    config, secrets = make_stores(tmp_path)
    fake = FakeQueue(job_id="job-img-1")
    node = TelegramSendImage(
        config_store=config,
        secret_store=secrets,
        client_factory=boom_client_factory,
        db_path=tmp_path / "history.sqlite3",
        queue_factory=lambda: fake,
    )
    image = frame()
    with caplog.at_level("INFO"):
        (result,) = node.publish(
            image,
            ACCOUNT_ID,
            DEST_ID,
            caption="hello {{prompt}}",
            prompt="a cat",
            wait_for_upload=False,
        )
    assert result is image
    assert len(fake.payloads) == 1
    payload = fake.payloads[0]
    assert payload.kind == "photo"
    assert payload.account_id == ACCOUNT_ID
    assert payload.destination_id == DEST_ID
    assert payload.chat_id == CHAT_ID
    assert len(payload.files) == 1
    data, filename = payload.files[0]
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert filename == "comfyui_8x8.png"
    assert payload.caption == "hello a cat"
    assert payload.protect_content is False
    assert payload.disable_notification is False
    assert payload.metadata["image_hash"] == sha256_hex(data)
    assert payload.metadata["filename"] == filename
    assert payload.metadata["prompt"] == "a cat"
    assert "job-img-1" in caplog.text  # job id logged
    assert FAKE_TOKEN not in caplog.text  # token never logged


def test_send_image_background_forwards_flags(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    fake = FakeQueue()
    node = TelegramSendImage(
        config_store=config,
        secret_store=secrets,
        client_factory=boom_client_factory,
        db_path=tmp_path / "history.sqlite3",
        queue_factory=lambda: fake,
    )
    node.publish(
        frame(),
        ACCOUNT_ID,
        DEST_ID,
        protect_content=True,
        disable_notification=True,
        wait_for_upload=False,
    )
    payload = fake.payloads[0]
    assert payload.protect_content is True
    assert payload.disable_notification is True


def test_send_image_background_duplicate_fails_fast(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    db_path = tmp_path / "history.sqlite3"
    fake = FakeQueue()
    node = TelegramSendImage(
        config_store=config,
        secret_store=secrets,
        client_factory=boom_client_factory,
        db_path=db_path,
        queue_factory=lambda: fake,
    )
    # Seed a success row with the same payload hash.
    expected_hash = sha256_hex(encode_image(frame(), format="png").data)
    db = Database(db_path)
    db.init_schema()
    PublishJobRepository(db).create(
        PublishJob(
            destination_id=DEST_ID, status="success", image_hash=expected_hash
        )
    )
    with pytest.raises(RuntimeError):
        node.publish(
            frame(), ACCOUNT_ID, DEST_ID, skip_duplicate=True,
            wait_for_upload=False,
        )
    assert fake.payloads == []  # refused BEFORE enqueue


def test_send_album_background_enqueues_album_payload(tmp_path: Path, caplog):
    config, secrets = make_stores(tmp_path)
    fake = FakeQueue(job_id="job-alb-1")
    node = TelegramSendAlbum(
        config_store=config,
        secret_store=secrets,
        client_factory=boom_client_factory,
        db_path=tmp_path / "history.sqlite3",
        queue_factory=lambda: fake,
    )
    batch = np.stack([frame(7), frame(8)])
    with caplog.at_level("INFO"):
        (result,) = node.publish(
            batch, ACCOUNT_ID, DEST_ID, caption="album {{prompt}}",
            prompt="cats", wait_for_upload=False,
        )
    assert result is batch
    assert len(fake.payloads) == 1
    payload = fake.payloads[0]
    assert payload.kind == "album"
    assert payload.account_id == ACCOUNT_ID
    assert payload.destination_id == DEST_ID
    assert payload.chat_id == CHAT_ID
    assert len(payload.files) == 2
    first_data, first_name = payload.files[0]
    assert first_data[:8] == b"\x89PNG\r\n\x1a\n"
    assert first_name == "comfyui_8x8.png"
    assert payload.caption == "album cats"
    assert payload.metadata["image_hash"] == sha256_hex(first_data)
    assert payload.metadata["filename"] == first_name
    assert "job-alb-1" in caplog.text
    assert FAKE_TOKEN not in caplog.text


def test_send_album_background_count_validation_still_synchronous(
    tmp_path: Path,
):
    config, secrets = make_stores(tmp_path)
    fake = FakeQueue()
    node = TelegramSendAlbum(
        config_store=config,
        secret_store=secrets,
        client_factory=boom_client_factory,
        db_path=tmp_path / "history.sqlite3",
        queue_factory=lambda: fake,
    )
    with pytest.raises(ValueError, match="2-10"):
        node.publish(frame(), ACCOUNT_ID, DEST_ID, wait_for_upload=False)
    assert fake.payloads == []


def test_sync_path_still_sends_via_transport(tmp_path: Path):
    """wait_for_upload=True is byte-identical to the old always-sync path."""
    from telegram.client import TelegramClient

    config, secrets = make_stores(tmp_path)
    record: list = []

    def fake_transport(url, *, files, data, timeout):
        record.append({"url": url, "files": files, "data": data})
        return 200, {
            "ok": True, "result": {"message_id": 42, "chat": {"id": -100123}}
        }

    def factory(token: str) -> TelegramClient:
        assert token == FAKE_TOKEN
        return TelegramClient(token, transport=fake_transport)

    node = TelegramSendImage(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path / "history.sqlite3",
        queue_factory=lambda: FakeQueue(),
    )
    image = frame()
    (result,) = node.publish(
        image, ACCOUNT_ID, DEST_ID, caption="hi", wait_for_upload=True
    )
    assert result is image
    assert len(record) == 1  # network used on the sync path


# ---------------------------------------------------------------------------
# Sender dispatch (fake TelegramClient, no transport/server needed)
# ---------------------------------------------------------------------------


class FakeTelegramClient:
    instances: list["FakeTelegramClient"] = []

    def __init__(self, token: str, *, retry_policy=None) -> None:
        self.token = token
        self.retry_policy = retry_policy
        self.photo_calls: list[dict] = []
        self.album_calls: list[dict] = []
        FakeTelegramClient.instances.append(self)

    def send_photo(
        self, chat_id, data, filename, caption=None, *,
        protect_content=False, disable_notification=False,
    ):
        self.photo_calls.append(
            {
                "chat_id": chat_id, "data": data, "filename": filename,
                "caption": caption, "protect_content": protect_content,
                "disable_notification": disable_notification,
            }
        )
        return SimpleNamespace(message_id=99)

    def send_media_group(self, chat_id, items, caption=None):
        self.album_calls.append(
            {"chat_id": chat_id, "items": items, "caption": caption}
        )
        return [11, 12]


@pytest.fixture
def fake_client(monkeypatch):
    FakeTelegramClient.instances.clear()
    monkeypatch.setattr(send_image_mod, "TelegramClient", FakeTelegramClient)
    monkeypatch.setattr(send_album_mod, "TelegramClient", FakeTelegramClient)
    return FakeTelegramClient


def test_sender_dispatch_photo_single_attempt(tmp_path: Path, fake_client):
    config, secrets = make_stores(tmp_path)
    sender = send_image_mod._make_sender(config, secrets)
    payload = make_payload(
        kind="photo",
        files=((b"img-bytes", "out.png"),),
        caption="cap",
        protect_content=True,
        disable_notification=True,
    )
    assert sender(payload) == "99"
    client = fake_client.instances[-1]
    assert client.token == FAKE_TOKEN
    assert client.retry_policy is None  # worker owns retries, no layering
    (call,) = client.photo_calls
    assert call["chat_id"] == CHAT_ID
    assert call["data"] == b"img-bytes"
    assert call["filename"] == "out.png"
    assert call["caption"] == "cap"
    assert call["protect_content"] is True
    assert call["disable_notification"] is True
    assert client.album_calls == []


def test_sender_dispatch_album_joined_ids(tmp_path: Path, fake_client):
    config, secrets = make_stores(tmp_path)
    sender = send_album_mod._make_sender(config, secrets)
    payload = make_payload(
        kind="album",
        files=((b"a", "a.png"), (b"b", "b.png")),
        caption="album-cap",
    )
    assert sender(payload) == "11,12"
    client = fake_client.instances[-1]
    assert client.token == FAKE_TOKEN
    assert client.retry_policy is None
    (call,) = client.album_calls
    assert call["chat_id"] == CHAT_ID
    assert call["items"] == [(b"a", "a.png"), (b"b", "b.png")]
    assert call["caption"] == "album-cap"


def test_sender_resolves_token_fresh_per_job(tmp_path: Path, fake_client):
    """Rotation-safe: a rotated secret is picked up on the next job."""
    config, secrets = make_stores(tmp_path)
    sender = send_image_mod._make_sender(config, secrets)
    sender(make_payload())
    assert fake_client.instances[-1].token == FAKE_TOKEN
    secrets.set_token(ACCOUNT_ID, "TEST_TOKEN_yyy")
    sender(make_payload())
    assert fake_client.instances[-1].token == "TEST_TOKEN_yyy"


def test_sender_unknown_kind_permanent(tmp_path: Path, fake_client):
    config, secrets = make_stores(tmp_path)
    sender = send_image_mod._make_sender(config, secrets)
    payload = make_payload(kind="photo")
    object.__setattr__(payload, "kind", "bogus")
    with pytest.raises(ValueError, match="unknown payload kind"):
        sender(payload)


# ---------------------------------------------------------------------------
# Shared queue lifecycle
# ---------------------------------------------------------------------------


def test_shared_queue_singleton_same_started_instance(tmp_path: Path):
    db = make_db(tmp_path, "shared.db")
    calls: list[int] = []

    def factory() -> PublishQueue:
        calls.append(1)
        return PublishQueue(db, lambda p: "m", poll_interval=0.05)

    key = f"test-singleton-{uuid.uuid4().hex}"
    first = get_shared_queue(key, factory)
    second = get_shared_queue(key, lambda: (_ for _ in ()).throw(AssertionError()))
    try:
        assert second is first
        assert len(calls) == 1  # factory called at most once per key
        assert first.worker_alive is True  # started automatically
    finally:
        _shutdown_shared_queues(timeout=5.0)
    assert first.worker_alive is False


def test_shared_queue_distinct_keys_distinct_queues(tmp_path: Path):
    db = make_db(tmp_path, "shared2.db")
    key_a = f"test-a-{uuid.uuid4().hex}"
    key_b = f"test-b-{uuid.uuid4().hex}"
    try:
        qa = get_shared_queue(
            key_a, lambda: PublishQueue(db, lambda p: "m", poll_interval=0.05)
        )
        qb = get_shared_queue(
            key_b, lambda: PublishQueue(db, lambda p: "m", poll_interval=0.05)
        )
        assert qa is not qb
    finally:
        _shutdown_shared_queues(timeout=5.0)


def test_shared_queue_rejects_bad_args(tmp_path: Path):
    with pytest.raises(ValueError):
        get_shared_queue("", lambda: None)  # type: ignore[return-value]
    with pytest.raises(ValueError):
        get_shared_queue("k", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        get_shared_queue(
            f"bad-factory-{uuid.uuid4().hex}", lambda: "not-a-queue"  # type: ignore[return-value]
        )


def test_shared_queue_atexit_hook_registered(monkeypatch):
    """The module registers its shutdown backstop with atexit on import.

    Loaded as an isolated probe module (never ``importlib.reload``: a
    reload would re-create ``PublishPayload`` and break ``isinstance``
    checks in already-imported test modules).
    """
    import atexit
    import importlib.util
    import sys

    calls: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn: calls.append(fn))
    path = Path(__file__).resolve().parents[1] / "services" / "queue.py"
    spec = importlib.util.spec_from_file_location(
        "services.queue_atexit_probe", path
    )
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    # Visible in sys.modules so dataclass string annotations resolve;
    # removed afterwards so the real services.queue module is untouched.
    sys.modules["services.queue_atexit_probe"] = probe
    try:
        spec.loader.exec_module(probe)
    finally:
        sys.modules.pop("services.queue_atexit_probe", None)
    assert calls == [probe._shutdown_shared_queues]


def test_entry_import_exposes_both_nodes():
    assert publisher_nodes.NODE_CLASS_MAPPINGS["Telegram Send Image"] is (
        TelegramSendImage
    )
    assert publisher_nodes.NODE_CLASS_MAPPINGS["Telegram Send Album"] is (
        TelegramSendAlbum
    )
