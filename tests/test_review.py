"""T063 review store + node integration, T064 approve/reject, T065 /run.

Fake transports and loopback-only local HTTP servers; ``TEST_TOKEN_xxx``
fakes only; no real Telegram/ComfyUI calls. The /run tests use a real
loopback ``http.server`` thread on an ephemeral port, so this module
does NOT install the session-wide urlopen block used elsewhere.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from publisher_nodes.send_album import TelegramSendAlbum
from publisher_nodes.send_image import TelegramSendImage
from services.commands import (
    BotContext,
    CommandRouter,
    IncomingCommand,
    register_builtins,
    register_review,
)
from services.review import (
    DEFAULT_REVIEW_DIRNAME,
    MAX_REVIEW_BYTES,
    ReviewStore,
    review_dir_for,
)
from storage.config import (
    Account,
    BotSettings,
    ConfigStore,
    Destination,
    FileSecretStore,
    Trigger,
)
from storage.database import Database
from storage.repositories import (
    GenerationRepository,
    PublishJob,
    PublishJobRepository,
)
from telegram.client import TelegramClient
from telegram.models import SendMessageResult, SendPhotoResult

FAKE_TOKEN = "TEST_TOKEN_xxx"
# Synthetic token-shaped value for leak assertions. Fake only.
LEAK_SHAPE = "999888:" + "B" * 35
ACCOUNT_ID = "acc1"
DEST_ID = "dst1"
DEST_NAME = "Channel"
CHAT_ID = "-100123"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


# ---------------------------------------------------------------------------
# Shared fakes/helpers
# ---------------------------------------------------------------------------


def make_stores(tmp_path: Path, token: str = FAKE_TOKEN):
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id=ACCOUNT_ID, name="Main bot"))
    config.add_destination(
        Destination(
            id=DEST_ID, name=DEST_NAME, account_id=ACCOUNT_ID, chat_id=CHAT_ID
        )
    )
    secrets.set_token(ACCOUNT_ID, token)
    return config, secrets


def make_repos(tmp_path: Path, name: str = "history.sqlite3"):
    db = Database(tmp_path / name)
    db.init_schema()
    return PublishJobRepository(db), GenerationRepository(db)


def frame(seed: int = 7, h: int = 8, w: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random((h, w, 3)).astype(np.float32)


class FakeNotifyClient:
    """Node-layer client: send_message captured, send_photo forbidden."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.photos: list = []

    def send_message(
        self, chat_id: str, text: str, *, timeout: float | None = None
    ) -> SendMessageResult:
        self.messages.append((chat_id, text))
        return SendMessageResult(message_id=len(self.messages), chat_id=chat_id)

    def send_photo(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("review path must not send media")


class FakeCmdClient:
    """Command-layer client: both send_photo and send_message scripted."""

    def __init__(self, photo_message_id: int = 77) -> None:
        self.photos: list[dict] = []
        self.messages: list[tuple[str, str]] = []
        self.photo_message_id = photo_message_id

    def send_photo(
        self,
        chat_id: str,
        image_bytes: bytes,
        filename: str,
        caption: Any = None,
        **kwargs: Any,
    ) -> SendPhotoResult:
        self.photos.append(
            {
                "chat_id": chat_id,
                "bytes": bytes(image_bytes),
                "filename": filename,
                "caption": caption,
            }
        )
        return SendPhotoResult(message_id=self.photo_message_id, chat_id=chat_id)

    def send_message(
        self, chat_id: str, text: str, *, timeout: float | None = None
    ) -> SendMessageResult:
        self.messages.append((chat_id, text))
        return SendMessageResult(message_id=len(self.messages), chat_id=chat_id)

    def get_updates(self, *args: Any, **kwargs: Any) -> list:
        return []


def make_router() -> CommandRouter:
    return register_review(register_builtins(CommandRouter()))


def make_ctx(
    tmp_path: Path,
    client: Any,
    jobs: PublishJobRepository,
    config: ConfigStore,
    secrets: Any,
    settings: BotSettings,
    review_dir: Path | None = None,
    gens: GenerationRepository | None = None,
) -> BotContext:
    return BotContext(
        client=client,
        config=config,
        secrets=secrets,
        jobs=jobs,
        generations=gens,
        queue=None,
        settings=settings,
        review_dir=review_dir if review_dir is not None else tmp_path / "review",
    )


def cmd(name: str, args: list[str], chat_id: str = "111") -> IncomingCommand:
    return IncomingCommand(
        name=name, args=args, chat_id=chat_id, from_id="9", update_id=1
    )


def enable_review(config: ConfigStore, admins: tuple[str, ...] = ("111",)) -> None:
    config.save_settings(
        BotSettings(review_mode=True, admin_chat_ids=admins)
    )


# ---------------------------------------------------------------------------
# ReviewStore unit tests
# ---------------------------------------------------------------------------


def test_review_dir_for(tmp_path: Path) -> None:
    assert review_dir_for(tmp_path / "history.sqlite3") == tmp_path / "review"
    assert DEFAULT_REVIEW_DIRNAME == "review"


def test_store_round_trip(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review")
    assert store.exists("job1") is False
    path = store.save("job1", PNG_BYTES)
    assert path == tmp_path / "review" / "job1.png"
    assert path.is_file()
    assert store.exists("job1") is True
    assert store.load("job1") == PNG_BYTES
    store.discard("job1")
    assert store.exists("job1") is False


@pytest.mark.parametrize(
    "bad",
    ["", "../evil", "..\\evil", "a/b", "a b", "bad!chars", "x" * 65, "a" * 0],
)
def test_store_rejects_unsafe_job_ids(tmp_path: Path, bad: str) -> None:
    store = ReviewStore(tmp_path / "review")
    with pytest.raises(ValueError):
        store.save(bad, PNG_BYTES)
    with pytest.raises(ValueError):
        store.load(bad)
    with pytest.raises(ValueError):
        store.exists(bad)
    with pytest.raises(ValueError):
        store.discard(bad)
    # Nothing escaped the review directory.
    assert list(tmp_path.iterdir()) == [] or not (tmp_path / "evil").exists()


@pytest.mark.parametrize("bad", [None, 42, b"bytes-id", ["job1"]])
def test_store_rejects_non_string_job_ids(tmp_path: Path, bad: Any) -> None:
    store = ReviewStore(tmp_path / "review")
    with pytest.raises(ValueError):
        store.save(bad, PNG_BYTES)


def test_store_rejects_non_png_and_empty(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review")
    with pytest.raises(ValueError):
        store.save("job1", b"")
    with pytest.raises(ValueError):
        store.save("job1", b"not a png payload")
    with pytest.raises(ValueError):
        store.save("job1", b"\x00" * 64)  # wrong magic
    with pytest.raises(ValueError):
        store.save("job1", "not-bytes")  # type: ignore[arg-type]
    assert store.exists("job1") is False


def test_store_rejects_oversize(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review")
    big = b"\x89PNG" + b"\x00" * MAX_REVIEW_BYTES
    assert len(big) > MAX_REVIEW_BYTES
    with pytest.raises(ValueError):
        store.save("job1", big)
    assert store.exists("job1") is False


def test_store_load_missing_raises_valueerror(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review")
    with pytest.raises(ValueError, match="review payload missing"):
        store.load("nope")


def test_store_discard_idempotent(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review")
    store.discard("ghost")  # missing_ok, never raises
    store.save("job1", PNG_BYTES)
    store.discard("job1")
    store.discard("job1")  # second discard still fine


# ---------------------------------------------------------------------------
# Node review path (T063)
# ---------------------------------------------------------------------------


def make_review_node(tmp_path: Path, fake: FakeNotifyClient) -> TelegramSendImage:
    config, secrets = make_stores(tmp_path)
    enable_review(config, ("111", "222"))

    def factory(token: str) -> Any:
        assert token == FAKE_TOKEN
        return fake

    return TelegramSendImage(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path / "history.sqlite3",
    )


def test_image_review_path_stages_without_sending(tmp_path: Path) -> None:
    fake = FakeNotifyClient()
    node = make_review_node(tmp_path, fake)
    img = frame()
    (result,) = node.publish(img, ACCOUNT_ID, DEST_ID, caption="hello review")
    assert result is img  # passthrough
    assert fake.photos == []  # no media sent
    assert [chat for chat, _ in fake.messages] == ["111", "222"]
    job_id: str | None = None
    for _, text in fake.messages:
        assert FAKE_TOKEN not in text
        assert "/approve " in text and "/reject " in text
        job_id = text.split("/approve ")[1].split()[0]
    assert job_id
    path = tmp_path / "review" / f"{job_id}.png"
    assert path.is_file()
    assert path.read_bytes()[:4] == b"\x89PNG"
    jobs, gens = make_repos(tmp_path)
    row = jobs.get(job_id)
    assert row is not None
    assert row.status == "pending_review"
    assert row.destination_id == DEST_ID
    assert row.attempts == 0
    assert row.caption == "hello review"
    assert row.filename and row.filename.endswith(".png")
    assert row.image_hash and len(row.image_hash) == 64
    assert len(gens.list_for_job(job_id)) == 1


def test_image_review_notifies_caption_preview_capped(tmp_path: Path) -> None:
    fake = FakeNotifyClient()
    node = make_review_node(tmp_path, fake)
    long_caption = "C" * 200
    node.publish(frame(), ACCOUNT_ID, DEST_ID, caption=long_caption)
    assert len(fake.messages) == 2
    for _, text in fake.messages:
        assert "C" * 80 in text
        assert "C" * 81 not in text


def test_image_review_ignores_background_flag(tmp_path: Path) -> None:
    fake = FakeNotifyClient()
    config, secrets = make_stores(tmp_path)
    enable_review(config)

    def factory(token: str) -> Any:
        return fake

    def _boom() -> Any:
        raise AssertionError("review path must not enqueue")

    node = TelegramSendImage(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path / "history.sqlite3",
        queue_factory=_boom,
    )
    (result,) = node.publish(
        frame(), ACCOUNT_ID, DEST_ID, caption="bg", wait_for_upload=False
    )
    assert result is not None
    assert fake.photos == []
    assert len(fake.messages) == 1  # one admin
    jobs, _ = make_repos(tmp_path)
    rows = jobs.list_recent(5)
    assert rows and rows[0].status == "pending_review"


def ok_transport(record: list) -> Any:
    def fake(url: str, *, files: Any, data: dict, timeout: float) -> Any:
        record.append({"url": url, "files": files, "data": data})
        return 200, {"ok": True, "result": {"message_id": 42, "chat": {"id": -100123}}}

    return fake


def test_review_off_by_default_normal_publish(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    assert config.get_settings().review_mode is False
    record: list = []

    def factory(token: str) -> TelegramClient:
        assert token == FAKE_TOKEN
        return TelegramClient(token, transport=ok_transport(record))

    node = TelegramSendImage(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path / "history.sqlite3",
    )
    (result,) = node.publish(frame(), ACCOUNT_ID, DEST_ID, caption="live")
    assert result is not None
    assert len(record) == 1  # media actually sent
    assert not (tmp_path / "review").exists()
    jobs, _ = make_repos(tmp_path)
    rows = jobs.list_recent(5)
    assert rows and rows[0].status == "success"


def test_settings_read_failure_fails_open(tmp_path: Path, caplog) -> None:
    config, secrets = make_stores(tmp_path)
    record: list = []

    class BrokenSettings(ConfigStore):
        def get_settings(self) -> BotSettings:  # type: ignore[override]
            raise RuntimeError("settings db locked")

    broken = BrokenSettings.__new__(BrokenSettings)
    # Reuse the populated stores' state via the real instance's dict.
    broken.__dict__.update(config.__dict__)

    def factory(token: str) -> TelegramClient:
        return TelegramClient(token, transport=ok_transport(record))

    node = TelegramSendImage(
        config_store=broken,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path / "history.sqlite3",
    )
    with caplog.at_level(logging.WARNING):
        (result,) = node.publish(frame(), ACCOUNT_ID, DEST_ID, caption="x")
    assert result is not None
    assert len(record) == 1  # normal publish proceeded
    assert "continuing with normal publish" in caplog.text


def test_album_review_path_stages_first_frame(tmp_path: Path) -> None:
    fake = FakeNotifyClient()
    config, secrets = make_stores(tmp_path)
    enable_review(config, ("111",))

    def factory(token: str) -> Any:
        return fake

    node = TelegramSendAlbum(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path / "history.sqlite3",
    )
    batch = np.stack([frame(7), frame(8)])
    (result,) = node.publish(batch, ACCOUNT_ID, DEST_ID, caption="album review")
    assert result is batch
    assert fake.photos == []
    assert len(fake.messages) == 1
    jobs, gens = make_repos(tmp_path)
    rows = jobs.list_recent(5)
    assert rows and rows[0].status == "pending_review"
    assert rows[0].caption == "album review"
    assert len(gens.list_for_job(rows[0].id)) == 1  # type: ignore[arg-type]
    assert (tmp_path / "review" / f"{rows[0].id}.png").is_file()


# ---------------------------------------------------------------------------
# Approve / reject (T064)
# ---------------------------------------------------------------------------


def seed_pending(
    tmp_path: Path,
    jobs: PublishJobRepository,
    job_id: str = "jobAbc_1-2",
    caption: str = "cap here",
    with_payload: bool = True,
) -> PublishJob:
    job = jobs.create(
        PublishJob(
            id=job_id,
            destination_id=DEST_ID,
            status="pending_review",
            image_hash="h" * 64,
            filename="comfyui_8x8.png",
            caption=caption,
            attempts=0,
        )
    )
    if with_payload:
        ReviewStore(tmp_path / "review").save(job_id, PNG_BYTES)
    return job


def seed_ctx(tmp_path: Path, client: Any, jobs, token: str = FAKE_TOKEN):
    config, secrets = make_stores(tmp_path, token)
    settings = BotSettings(review_mode=True, admin_chat_ids=("111",))
    return make_ctx(tmp_path, client, jobs, config, secrets, settings)


def test_approve_happy(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    seed_pending(tmp_path, jobs)
    client = FakeCmdClient()
    ctx = seed_ctx(tmp_path, client, jobs)
    reply = make_router().handle(ctx, cmd("approve", ["jobAbc_1-2"]))
    assert reply == "published to Channel, message 77"
    assert len(client.photos) == 1
    sent = client.photos[0]
    assert sent["chat_id"] == CHAT_ID
    assert sent["bytes"] == PNG_BYTES
    assert sent["filename"] == "comfyui_8x8.png"
    assert sent["caption"] == "cap here"
    row = jobs.get("jobAbc_1-2")
    assert row is not None and row.status == "success"
    assert row.telegram_message_id == "77"
    assert ReviewStore(tmp_path / "review").exists("jobAbc_1-2") is False


def test_approve_defaults_filename_and_no_caption(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    jobs.create(
        PublishJob(
            id="j1", destination_id=DEST_ID, status="pending_review", attempts=0
        )
    )
    ReviewStore(tmp_path / "review").save("j1", PNG_BYTES)
    client = FakeCmdClient()
    ctx = seed_ctx(tmp_path, client, jobs)
    reply = make_router().handle(ctx, cmd("approve", ["j1"]))
    assert reply == "published to Channel, message 77"
    assert client.photos[0]["filename"] == "review_j1.png"
    assert client.photos[0]["caption"] is None


@pytest.mark.parametrize("arg", ["../evil", "a/b", "bad!x", "x" * 65, ""])
def test_approve_invalid_or_unknown_job(tmp_path: Path, arg: str) -> None:
    jobs, _ = make_repos(tmp_path)
    client = FakeCmdClient()
    ctx = seed_ctx(tmp_path, client, jobs)
    router = make_router()
    assert router.handle(ctx, cmd("approve", [arg])) == f"unknown review job '{arg}'"
    assert router.handle(ctx, cmd("approve", ["ghost42"])) == (
        "unknown review job 'ghost42'"
    )
    assert client.photos == []


def test_approve_missing_arg(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    ctx = seed_ctx(tmp_path, FakeCmdClient(), jobs)
    assert make_router().handle(ctx, cmd("approve", [])) == "unknown review job ''"


def test_approve_already_handled(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    jobs.create(
        PublishJob(id="done1", destination_id=DEST_ID, status="success", attempts=1)
    )
    ctx = seed_ctx(tmp_path, FakeCmdClient(), jobs)
    reply = make_router().handle(ctx, cmd("approve", ["done1"]))
    assert reply == "already handled (status success)"


def test_approve_missing_payload_marks_failed(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    seed_pending(tmp_path, jobs, with_payload=False)
    client = FakeCmdClient()
    ctx = seed_ctx(tmp_path, client, jobs)
    reply = make_router().handle(ctx, cmd("approve", ["jobAbc_1-2"]))
    assert reply == "review payload missing; job marked failed"
    row = jobs.get("jobAbc_1-2")
    assert row is not None and row.status == "failed"
    assert row.error_code == "PayloadLost"
    assert client.photos == []


def test_approve_send_failure_raises_generic(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    seed_pending(tmp_path, jobs)

    class BoomClient(FakeCmdClient):
        def send_photo(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("telegram down")

    ctx = seed_ctx(tmp_path, BoomClient(), jobs)
    assert (
        make_router().handle(ctx, cmd("approve", ["jobAbc_1-2"]))
        == "error processing command"
    )


def test_reject_happy_and_idempotent(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    seed_pending(tmp_path, jobs)
    client = FakeCmdClient()
    ctx = seed_ctx(tmp_path, client, jobs)
    router = make_router()
    assert router.handle(ctx, cmd("reject", ["jobAbc_1-2"])) == (
        "rejected publish to Channel"
    )
    row = jobs.get("jobAbc_1-2")
    assert row is not None and row.status == "failed"
    assert row.error_code == "Rejected"
    assert ReviewStore(tmp_path / "review").exists("jobAbc_1-2") is False
    assert client.photos == []  # reject never sends media
    # Second call: already handled, terminal status named.
    assert router.handle(ctx, cmd("reject", ["jobAbc_1-2"])) == (
        "already handled (status failed)"
    )


def test_reject_unknown_job(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    ctx = seed_ctx(tmp_path, FakeCmdClient(), jobs)
    assert make_router().handle(ctx, cmd("reject", ["ghost"])) == (
        "unknown review job 'ghost'"
    )


def test_mutating_handlers_reject_non_admin(tmp_path: Path) -> None:
    jobs, _ = make_repos(tmp_path)
    seed_pending(tmp_path, jobs)
    client = FakeCmdClient()
    ctx = seed_ctx(tmp_path, client, jobs)
    router = make_router()
    for name, args in (
        ("approve", ["jobAbc_1-2"]),
        ("reject", ["jobAbc_1-2"]),
        ("run", ["wf"]),
    ):
        assert router.handle(ctx, cmd(name, args, chat_id="666")) == "not authorized"
    assert client.photos == []
    assert jobs.get("jobAbc_1-2") is not None
    assert jobs.get("jobAbc_1-2").status == "pending_review"  # type: ignore[union-attr]


def test_token_shaped_values_never_in_replies_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    jobs, gens = make_repos(tmp_path)
    seed_pending(tmp_path, jobs, caption="prefix " + LEAK_SHAPE + " suffix")
    client = FakeCmdClient()
    ctx = seed_ctx(tmp_path, client, jobs, token=LEAK_SHAPE)
    router = make_router()
    with caplog.at_level(logging.DEBUG):
        replies = [
            router.handle(ctx, cmd("approve", ["jobAbc_1-2"])),
            router.handle(ctx, cmd("reject", ["jobAbc_1-2"])),
            router.handle(ctx, cmd("run", ["wf"])),
        ]
    for reply in replies:
        assert reply is not None and LEAK_SHAPE not in reply
    assert LEAK_SHAPE not in caplog.text


# ---------------------------------------------------------------------------
# /run trigger (T065): loopback fake ComfyUI server
# ---------------------------------------------------------------------------


def start_fake_comfy(mode: str = "ok"):
    received: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received.append({"path": self.path, "body": body})
            if mode == "error":
                data = json.dumps({"error": "boom"}).encode()
                self.send_response(500)
            elif mode == "malformed":
                data = json.dumps({"nope": 1}).encode()
                self.send_response(200)
            else:
                data = json.dumps({"prompt_id": "pfx-123"}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


def write_prompt(tmp_path: Path, name: str = "wf.json", payload: Any = None) -> str:
    if payload is None:
        payload = {"1": {"class_type": "EmptyImage", "inputs": {}}}
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def run_ctx(
    tmp_path: Path,
    triggers: tuple,
    host: str = "127.0.0.1",
    port: int = 8188,
    admins: tuple[str, ...] = ("111",),
) -> BotContext:
    config, secrets = make_stores(tmp_path)
    jobs, _ = make_repos(tmp_path)
    settings = BotSettings(
        admin_chat_ids=admins, comfy_host=host, comfy_port=port, triggers=triggers
    )
    return make_ctx(tmp_path, FakeCmdClient(), jobs, config, secrets, settings)


def test_run_happy(tmp_path: Path) -> None:
    server, received = start_fake_comfy("ok")
    try:
        port = server.server_address[1]
        prompt_file = write_prompt(tmp_path)
        ctx = run_ctx(tmp_path, (Trigger(name="wf", prompt_file=prompt_file),), port=port)
        reply = make_router().handle(ctx, cmd("run", ["wf"]))
        assert reply == "workflow started: pfx-123"
        assert len(received) == 1
        assert received[0]["path"] == "/prompt"
        body = json.loads(received[0]["body"].decode("utf-8"))
        assert "prompt" in body and body["prompt"]["1"]["class_type"] == "EmptyImage"
    finally:
        server.shutdown()
        server.server_close()


def test_run_usage_and_unknown(tmp_path: Path) -> None:
    prompt_file = write_prompt(tmp_path)
    ctx = run_ctx(tmp_path, (Trigger(name="wf", prompt_file=prompt_file),))
    router = make_router()
    assert router.handle(ctx, cmd("run", [])) == "usage: /run <name>"
    reply = router.handle(ctx, cmd("run", ["nope"]))
    assert "unknown trigger 'nope'" in reply


def test_run_empty_triggers(tmp_path: Path) -> None:
    ctx = run_ctx(tmp_path, ())
    reply = make_router().handle(ctx, cmd("run", ["wf"]))
    assert "no triggers configured" in reply
    assert "unknown trigger 'wf'" in reply


def test_run_missing_prompt_file(tmp_path: Path) -> None:
    ctx = run_ctx(
        tmp_path, (Trigger(name="wf", prompt_file=str(tmp_path / "absent.json")),)
    )
    assert make_router().handle(ctx, cmd("run", ["wf"])) == (
        "trigger misconfigured: prompt file missing"
    )


def test_run_directory_as_prompt_file(tmp_path: Path) -> None:
    ctx = run_ctx(tmp_path, (Trigger(name="wf", prompt_file=str(tmp_path)),))
    assert make_router().handle(ctx, cmd("run", ["wf"])) == (
        "trigger misconfigured: prompt file missing"
    )


def test_run_oversize_prompt_file_refused(tmp_path: Path) -> None:
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (5 * 1024 * 1024 + 1))
    ctx = run_ctx(tmp_path, (Trigger(name="wf", prompt_file=str(big)),))
    assert make_router().handle(ctx, cmd("run", ["wf"])) == (
        "trigger misconfigured: prompt file too large"
    )


def test_run_invalid_json_prompt_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    ctx = run_ctx(tmp_path, (Trigger(name="wf", prompt_file=str(bad)),))
    reply = make_router().handle(ctx, cmd("run", ["wf"]))
    assert reply.startswith("trigger failed")


def test_run_non_loopback_host_rejected(tmp_path: Path) -> None:
    prompt_file = write_prompt(tmp_path)
    # Constructed directly: save-time validation would refuse this host,
    # so the handler's own re-validation is the guard under test.
    settings = BotSettings(
        admin_chat_ids=("111",),
        comfy_host="example.com",
        comfy_port=8188,
        triggers=(Trigger(name="wf", prompt_file=prompt_file),),
    )
    config, secrets = make_stores(tmp_path)
    jobs, _ = make_repos(tmp_path)
    ctx = make_ctx(tmp_path, FakeCmdClient(), jobs, config, secrets, settings)
    reply = make_router().handle(ctx, cmd("run", ["wf"]))
    assert "loopback" in reply


def test_run_server_error_and_malformed(tmp_path: Path) -> None:
    for mode in ("error", "malformed"):
        home = tmp_path / mode
        home.mkdir()
        server, _ = start_fake_comfy(mode)
        try:
            port = server.server_address[1]
            prompt_file = write_prompt(home)
            ctx = run_ctx(
                home, (Trigger(name="wf", prompt_file=prompt_file),), port=port
            )
            reply = make_router().handle(ctx, cmd("run", ["wf"]))
            assert reply.startswith("trigger failed"), (mode, reply)
        finally:
            server.shutdown()
            server.server_close()


def test_run_unreachable_server(tmp_path: Path) -> None:
    server, _ = start_fake_comfy("ok")
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    prompt_file = write_prompt(tmp_path)
    ctx = run_ctx(tmp_path, (Trigger(name="wf", prompt_file=prompt_file),), port=port)
    reply = make_router().handle(ctx, cmd("run", ["wf"]))
    assert reply.startswith("trigger failed")
