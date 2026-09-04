"""T060 receiver + T061 /status + T062 /queue. Fake transports only.

No real Telegram calls: ``urllib.request.urlopen`` is blocked session-wide
and every client here is either a recording fake or a ``TelegramClient``
with an injected fake transport. All tokens are fakes (``TEST_TOKEN_xxx``).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from services.commands import (
    BotContext,
    CommandReceiver,
    CommandRouter,
    IncomingCommand,
    OffsetStore,
    parse_command,
    register_builtins,
)
from services.queue import PublishQueue
from storage.config import BotSettings, ConfigStore
from storage.database import Database
from storage.repositories import GenerationRepository, PublishJob, PublishJobRepository
from telegram.client import TelegramClient
from telegram.errors import AuthenticationError, TransientTelegramError
from telegram.models import SendMessageResult, parse_update

FAKE_TOKEN = "TEST_TOKEN_xxx"
# Synthetic token-shaped value for leak assertions. Fake only.
LEAK_SHAPE = "999888:" + "B" * 35


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("tests must not make real network calls")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Recording stand-in for TelegramClient (get_updates + send_message)."""

    def __init__(
        self,
        updates: list[dict] | None = None,
        *,
        fail_chats: tuple[str, ...] = (),
        fail_get: BaseException | None = None,
    ) -> None:
        self.updates = list(updates or [])
        self.sent: list[tuple[str, str]] = []
        self.get_calls: list[dict] = []
        self.fail_chats = set(fail_chats)
        self.fail_get = fail_get

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int | float = 30,
        *,
        client_timeout: float | None = None,
    ) -> list[dict]:
        self.get_calls.append(
            {"offset": offset, "timeout": timeout, "client_timeout": client_timeout}
        )
        if self.fail_get is not None:
            raise self.fail_get
        if offset is None:
            return list(self.updates)
        out = []
        for raw in self.updates:
            if (
                isinstance(raw, dict)
                and isinstance(raw.get("update_id"), int)
                and not isinstance(raw.get("update_id"), bool)
                and raw["update_id"] >= offset
            ):
                out.append(raw)
        return out

    def send_message(
        self, chat_id: str, text: str, *, timeout: float | None = None
    ) -> SendMessageResult:
        if chat_id in self.fail_chats:
            raise TransientTelegramError("boom")
        self.sent.append((chat_id, text))
        return SendMessageResult(message_id=len(self.sent), chat_id=chat_id)


def make_update(
    uid: int, chat: Any, text: Any, from_id: Any = "999"
) -> dict:
    msg: dict[str, Any] = {
        "message_id": 1,
        "chat": {"id": chat, "type": "private"},
        "text": text,
    }
    if from_id is not None:
        msg["from"] = {"id": from_id}
    return {"update_id": uid, "message": msg}


def make_repos(tmp_path: Path) -> tuple[PublishJobRepository, GenerationRepository]:
    db = Database(tmp_path / "history.sqlite3")
    db.init_schema()
    return PublishJobRepository(db), GenerationRepository(db)


def make_ctx(
    tmp_path: Path,
    client: Any,
    jobs: PublishJobRepository,
    gens: GenerationRepository | None = None,
    queue: Any = "real",
    settings: BotSettings | None = None,
) -> BotContext:
    if queue == "real":
        db = Database(tmp_path / "queue.sqlite3")
        db.init_schema()
        queue = PublishQueue(db, sender=lambda p: "1")
    return BotContext(
        client=client,
        config=ConfigStore(tmp_path / "config.json"),
        secrets=None,  # type: ignore[arg-type]
        jobs=jobs,
        generations=gens,
        queue=queue,
        settings=settings
        if settings is not None
        else BotSettings(admin_chat_ids=("123",)),
        review_dir=tmp_path,
    )


def make_router() -> CommandRouter:
    return register_builtins(CommandRouter())


def seed_job(
    jobs: PublishJobRepository,
    destination_id: str = "dst1",
    status: str = "queued",
    caption: str | None = "hello",
    attempts: int = 0,
) -> PublishJob:
    return jobs.create(
        PublishJob(
            destination_id=destination_id,
            status=status,
            caption=caption,
            attempts=attempts,
        )
    )


# ---------------------------------------------------------------------------
# parse_command table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/status", ("status", [])),
        ("/status@MyBot", ("status", [])),
        ("/STATUS x Y", ("status", ["x", "Y"])),
        ("/run arg1 arg2", ("run", ["arg1", "arg2"])),
        ("  /queue  ", ("queue", [])),
        ("/queue extra   spaces", ("queue", ["extra", "spaces"])),
        ("/approve abc123", ("approve", ["abc123"])),
        ("/", None),
        ("/  ", None),
        ("/@bot", None),
        ("", None),
        ("   ", None),
        ("hello", None),
        ("status (no slash)", None),
        ("//status", ("/status", [])),  # unknown command -> router stays quiet
        (None, None),
        (123, None),
        (b"/status", None),
        (["/status"], None),
    ],
)
def test_parse_command_table(text: Any, expected: Any) -> None:
    assert parse_command(text) == expected  # type: ignore[arg-type]


def test_parse_command_never_raises() -> None:
    class Evil:
        def __str__(self) -> str:
            raise RuntimeError("nope")

        def strip(self) -> str:
            raise RuntimeError("nope")

    assert parse_command(Evil()) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_update (models) spot checks through the receiver's lens
# ---------------------------------------------------------------------------


def test_parse_update_valid_and_coercions() -> None:
    upd = parse_update(make_update(7, 12345, "/status", from_id=999))
    assert upd is not None
    assert (upd.update_id, upd.chat_id, upd.text, upd.from_id) == (
        7,
        "12345",
        "/status",
        "999",
    )
    assert parse_update(make_update(8, "@chan", "hi", from_id=None)) is not None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "string",
        42,
        [],
        {},
        {"update_id": 1},  # no message
        {"update_id": 1, "message": "nope"},
        {"update_id": 1, "message": {"chat": {"id": "1"}}},  # no text
        {"update_id": 1, "message": {"chat": {"id": "1"}, "text": 42}},
        {"update_id": 1, "message": {"chat": {"id": "1"}, "text": None}},
        {"update_id": 1, "message": {"text": "hi"}},  # no chat
        {"update_id": 1, "message": {"chat": {}, "text": "hi"}},
        {"update_id": 1, "message": {"chat": {"id": ""}, "text": "hi"}},
        {"update_id": "1", "message": {"chat": {"id": "1"}, "text": "hi"}},
        {"update_id": True, "message": {"chat": {"id": "1"}, "text": "hi"}},
        # Non-message update kinds are ignored, never errors:
        {"update_id": 2, "edited_message": {"chat": {"id": "1"}, "text": "hi"}},
        {"update_id": 3, "callback_query": {"data": "/status"}},
        {"update_id": 4, "channel_post": {"chat": {"id": "1"}, "text": "hi"}},
    ],
)
def test_parse_update_rejects_non_message(raw: Any) -> None:
    assert parse_update(raw) is None


# ---------------------------------------------------------------------------
# Router paths
# ---------------------------------------------------------------------------


def test_router_unknown_command_returns_none(tmp_path: Path) -> None:
    router = make_router()
    jobs, gens = make_repos(tmp_path)
    ctx = make_ctx(tmp_path, FakeClient(), jobs, gens)
    cmd = IncomingCommand(
        name="nope", args=[], chat_id="123", from_id="9", update_id=1
    )
    assert router.handle(ctx, cmd) is None


def test_router_register_duplicate_and_invalid_raise() -> None:
    router = CommandRouter()
    router.register("status", lambda ctx, cmd: "ok")
    with pytest.raises(ValueError):
        router.register("status", lambda ctx, cmd: "again")
    with pytest.raises(ValueError):
        router.register("STATUS", lambda ctx, cmd: "case-insensitive dup")
    for bad in ("", "   ", None, 42):
        with pytest.raises(ValueError):
            router.register(bad, lambda ctx, cmd: "ok")  # type: ignore[arg-type]
    for bad_handler in (None, "x", 42):
        with pytest.raises(ValueError):
            router.register("other", bad_handler)  # type: ignore[arg-type]


def test_router_handler_exception_becomes_fixed_error_reply(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    router = CommandRouter()

    def _boom(ctx: BotContext, cmd: IncomingCommand) -> str:
        raise RuntimeError("kaboom " + LEAK_SHAPE)

    router.register("boom", _boom)
    jobs, gens = make_repos(tmp_path)
    ctx = make_ctx(tmp_path, FakeClient(), jobs, gens)
    cmd = IncomingCommand(
        name="boom", args=[], chat_id="123", from_id="9", update_id=1
    )
    with caplog.at_level(logging.WARNING, logger="services.commands"):
        reply = router.handle(ctx, cmd)
    assert reply == "error processing command"
    assert "kaboom" not in (reply or "")
    assert LEAK_SHAPE not in (reply or "")
    assert LEAK_SHAPE not in caplog.text


def test_router_non_string_result_becomes_error_reply(tmp_path: Path) -> None:
    router = CommandRouter()
    router.register("weird", lambda ctx, cmd: 42)  # type: ignore[return-value]
    jobs, gens = make_repos(tmp_path)
    ctx = make_ctx(tmp_path, FakeClient(), jobs, gens)
    cmd = IncomingCommand(
        name="weird", args=[], chat_id="123", from_id="9", update_id=1
    )
    assert router.handle(ctx, cmd) == "error processing command"


def test_router_none_result_means_no_reply(tmp_path: Path) -> None:
    router = CommandRouter()
    router.register("quiet", lambda ctx, cmd: None)  # type: ignore[return-value]
    jobs, gens = make_repos(tmp_path)
    ctx = make_ctx(tmp_path, FakeClient(), jobs, gens)
    cmd = IncomingCommand(
        name="quiet", args=[], chat_id="123", from_id="9", update_id=1
    )
    assert router.handle(ctx, cmd) is None


# ---------------------------------------------------------------------------
# Built-in handlers: help / status / queue
# ---------------------------------------------------------------------------


def test_help_lists_commands(tmp_path: Path) -> None:
    router = make_router()
    client = FakeClient([make_update(1, "123", "/help")])
    jobs, gens = make_repos(tmp_path)
    ctx = make_ctx(tmp_path, client, jobs, gens)
    receiver = CommandReceiver(
        client, router, ("123",), OffsetStore(tmp_path / "offset.json")
    )
    sent = receiver.poll_once(ctx)
    assert len(sent) == 1
    assert sent[0][0] == "123"
    for name in ("help", "status", "queue"):
        assert f"/{name}" in sent[0][1]


def test_status_content_counts_worker_and_recent(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    seed_job(jobs, "dst1", "queued")
    seed_job(jobs, "dst2", "success")
    seed_job(jobs, "dst3", "failed")
    client = FakeClient([make_update(1, "123", "/status")])
    ctx = make_ctx(tmp_path, client, jobs, gens)  # real queue, not started
    receiver = CommandReceiver(
        client, make_router(), ("123",), OffsetStore(tmp_path / "offset.json")
    )
    sent = receiver.poll_once(ctx)
    assert len(sent) == 1
    reply = sent[0][1]
    assert "queued=1" in reply
    assert "success=1" in reply
    assert "failed=1" in reply
    assert "worker=stopped" in reply
    assert "recent: " in reply and "dst3" in reply
    assert FAKE_TOKEN not in reply


def test_status_with_queue_none_reports_off(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    client = FakeClient([make_update(1, "123", "/status")])
    ctx = make_ctx(tmp_path, client, jobs, gens, queue=None)
    receiver = CommandReceiver(
        client, make_router(), ("123",), OffsetStore(tmp_path / "offset.json")
    )
    sent = receiver.poll_once(ctx)
    assert "worker=off" in sent[0][1]
    assert "recent: none" in sent[0][1]


def test_queue_lists_and_truncates_captions(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    long_caption = "C" * 200
    job = seed_job(jobs, "dst1", "queued", caption=long_caption, attempts=2)
    seed_job(jobs, "dst2", "success", caption="done-caption")
    client = FakeClient([make_update(1, "123", "/queue")])
    ctx = make_ctx(tmp_path, client, jobs, gens)
    receiver = CommandReceiver(
        client, make_router(), ("123",), OffsetStore(tmp_path / "offset.json")
    )
    sent = receiver.poll_once(ctx)
    reply = sent[0][1]
    assert job.id in reply
    assert "dst1" in reply
    assert "attempts=2" in reply
    assert "C" * 80 in reply
    assert "C" * 81 not in reply  # truncated at exactly 80 chars
    assert "done-caption" not in reply  # terminal jobs are not listed


def test_queue_empty_message(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    client = FakeClient([make_update(1, "123", "/queue")])
    ctx = make_ctx(tmp_path, client, jobs, gens)
    receiver = CommandReceiver(
        client, make_router(), ("123",), OffsetStore(tmp_path / "offset.json")
    )
    sent = receiver.poll_once(ctx)
    assert sent[0][1] == "queue is empty"


def test_queue_reply_masks_token_shaped_caption(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    seed_job(jobs, "dst1", "queued", caption="prefix " + LEAK_SHAPE + " suffix")
    client = FakeClient([make_update(1, "123", "/queue")])
    ctx = make_ctx(tmp_path, client, jobs, gens)
    receiver = CommandReceiver(
        client, make_router(), ("123",), OffsetStore(tmp_path / "offset.json")
    )
    sent = receiver.poll_once(ctx)
    assert LEAK_SHAPE not in sent[0][1]
    assert "***" in sent[0][1]


# ---------------------------------------------------------------------------
# Allowlist gate + fail-closed
# ---------------------------------------------------------------------------


def test_non_allowlisted_chat_gets_no_reply_but_advances_offset(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    jobs, gens = make_repos(tmp_path)
    client = FakeClient(
        [
            make_update(10, "666", "/status"),  # stranger
            make_update(11, "123", "/status"),  # admin
        ]
    )
    ctx = make_ctx(tmp_path, client, jobs, gens)
    store = OffsetStore(tmp_path / "offset.json")
    receiver = CommandReceiver(client, make_router(), ("123",), store)
    with caplog.at_level(logging.WARNING, logger="services.commands"):
        sent = receiver.poll_once(ctx)
    assert len(sent) == 1 and sent[0][0] == "123"
    assert client.sent and all(chat == "123" for chat, _ in client.sent)
    assert store.get() == 12  # skipped update still advances the offset
    assert "666" in caplog.text  # chat_id only in the warning


def test_empty_allowlist_is_fail_closed_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    jobs, gens = make_repos(tmp_path)
    seed_job(jobs, "dst1", "queued")
    client = FakeClient([make_update(1, "123", "/status")])
    ctx = make_ctx(tmp_path, client, jobs, gens)
    store = OffsetStore(tmp_path / "offset.json")
    receiver = CommandReceiver(client, make_router(), (), store)
    with caplog.at_level(logging.WARNING, logger="services.commands"):
        sent = receiver.poll_once(ctx)
    assert sent == []
    assert client.sent == []  # no replies: no oracle for scanners
    assert store.get() == 2  # offset still advances past ignored updates


def test_unknown_text_and_unknown_command_stay_quiet(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    client = FakeClient(
        [
            make_update(1, "123", "just chatting"),
            make_update(2, "123", "/frobnicate x"),
        ]
    )
    ctx = make_ctx(tmp_path, client, jobs, gens)
    store = OffsetStore(tmp_path / "offset.json")
    receiver = CommandReceiver(client, make_router(), ("123",), store)
    assert receiver.poll_once(ctx) == []
    assert client.sent == []
    assert store.get() == 3


def test_numeric_chat_id_matches_string_allowlist(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    client = FakeClient([make_update(1, 123, "/queue")])  # int chat id (FR-002)
    ctx = make_ctx(tmp_path, client, jobs, gens)
    receiver = CommandReceiver(
        client, make_router(), ("123",), OffsetStore(tmp_path / "offset.json")
    )
    sent = receiver.poll_once(ctx)
    assert len(sent) == 1 and sent[0][0] == "123"


# ---------------------------------------------------------------------------
# Offset advance + persistence
# ---------------------------------------------------------------------------


def test_offset_advances_past_skipped_and_malformed(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    client = FakeClient(
        [
            make_update(20, "123", "/help"),
            {"update_id": 21, "edited_message": {"text": "x"}},  # skipped
            {"update_id": 22},  # skipped (no message)
            "garbage-entry",  # skipped (not a dict)
            make_update(23, "123", "not a command"),  # skipped (no reply)
        ]
    )
    ctx = make_ctx(tmp_path, client, jobs, gens)
    store = OffsetStore(tmp_path / "offset.json")
    receiver = CommandReceiver(client, make_router(), ("123",), store)
    sent = receiver.poll_once(ctx)
    assert len(sent) == 1
    assert store.get() == 24
    # Second poll sees nothing new (offset filters) and persists nothing new.
    assert receiver.poll_once(ctx) == []
    assert client.get_calls[-1]["offset"] == 24
    assert store.get() == 24


def test_empty_results_persist_nothing(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    client = FakeClient([])
    ctx = make_ctx(tmp_path, client, jobs, gens)
    store = OffsetStore(tmp_path / "offset.json")
    receiver = CommandReceiver(client, make_router(), ("123",), store)
    assert receiver.poll_once(ctx) == []
    assert store.get() is None
    assert not (tmp_path / "offset.json").exists()


def test_corrupt_offset_store_raises_loudly(tmp_path: Path) -> None:
    (tmp_path / "offset.json").write_text("{not json", encoding="utf-8")
    store = OffsetStore(tmp_path / "offset.json")
    with pytest.raises(ValueError):
        store.get()
    jobs, gens = make_repos(tmp_path)
    ctx = make_ctx(tmp_path, FakeClient([]), jobs, gens)
    receiver = CommandReceiver(ctx.client, make_router(), ("123",), store)
    with pytest.raises(ValueError):
        receiver.poll_once(ctx)


# ---------------------------------------------------------------------------
# OffsetStore unit tests
# ---------------------------------------------------------------------------


def test_offset_store_missing_returns_none(tmp_path: Path) -> None:
    assert OffsetStore(tmp_path / "nope.json").get() is None


def test_offset_store_round_trip(tmp_path: Path) -> None:
    store = OffsetStore(tmp_path / "sub" / "offset.json")
    store.set(41)
    assert store.get() == 41
    assert json.loads((tmp_path / "sub" / "offset.json").read_text()) == {
        "offset": 41
    }


@pytest.mark.parametrize(
    "payload",
    [
        "{bad json",
        "[1, 2]",
        '"str"',
        "42",
        "{}",
        '{"offset": "41"}',
        '{"offset": True}',
        '{"offset": -1}',
        '{"offset": 4.5}',
        '{"other": 1}',
    ],
)
def test_offset_store_corrupt_raises_valueerror(
    tmp_path: Path, payload: str
) -> None:
    (tmp_path / "offset.json").write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        OffsetStore(tmp_path / "offset.json").get()


@pytest.mark.parametrize("bad", [-1, "41", 4.5, True, None])
def test_offset_store_set_rejects_bad_values(
    tmp_path: Path, bad: Any
) -> None:
    with pytest.raises(ValueError):
        OffsetStore(tmp_path / "offset.json").set(bad)


# ---------------------------------------------------------------------------
# Per-reply failure isolation
# ---------------------------------------------------------------------------


def test_one_failed_reply_does_not_abort_others(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    client = FakeClient(
        [
            make_update(1, "aaa", "/queue"),
            make_update(2, "bbb", "/queue"),  # send fails here
            make_update(3, "ccc", "/status"),
        ],
        fail_chats=("bbb",),
    )
    settings = BotSettings(admin_chat_ids=("aaa", "bbb", "ccc"))
    ctx = make_ctx(tmp_path, client, jobs, gens, settings=settings)
    receiver = CommandReceiver(
        client,
        make_router(),
        ("aaa", "bbb", "ccc"),
        OffsetStore(tmp_path / "offset.json"),
    )
    sent = receiver.poll_once(ctx)
    assert [chat for chat, _ in sent] == ["aaa", "ccc"]
    assert [chat for chat, _ in client.sent] == ["aaa", "ccc"]


# ---------------------------------------------------------------------------
# Security: token never in replies/logs; disabled by default
# ---------------------------------------------------------------------------


def test_replies_and_logs_never_carry_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    jobs, gens = make_repos(tmp_path)
    seed_job(jobs, "dst1", "queued", caption="some caption")
    client = FakeClient(
        [
            make_update(1, "123", "/status"),
            make_update(2, "123", "/queue"),
            make_update(3, "123", "/help"),
            make_update(4, "stranger", FAKE_TOKEN),  # ignored, silent
        ]
    )
    ctx = make_ctx(tmp_path, client, jobs, gens)
    receiver = CommandReceiver(
        client, make_router(), ("123",), OffsetStore(tmp_path / "offset.json")
    )
    with caplog.at_level(logging.DEBUG, logger="services.commands"):
        sent = receiver.poll_once(ctx)
    assert len(sent) == 3
    for _, reply in sent:
        assert FAKE_TOKEN not in reply
    assert FAKE_TOKEN not in caplog.text


def test_receiver_creates_no_threads_and_polls_nothing(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    before = set(threading.enumerate())
    client = FakeClient([])
    ctx = make_ctx(tmp_path, client, jobs, gens)
    CommandReceiver(
        client, make_router(), ("123",), OffsetStore(tmp_path / "offset.json")
    )
    assert set(threading.enumerate()) == before
    assert client.get_calls == []  # nothing polled at construction


# ---------------------------------------------------------------------------
# End-to-end via a real TelegramClient with a scripted fake transport
# ---------------------------------------------------------------------------


def make_transport(script_updates: list[dict] | None, *, status: int = 200):
    calls: list[dict] = []

    def fake(url: str, *, files: Any, data: dict, timeout: float):
        calls.append({"url": url, "data": dict(data), "timeout": timeout})
        assert FAKE_TOKEN in url
        if "getUpdates" in url:
            if status != 200:
                return status, {
                    "ok": False,
                    "error_code": status,
                    "description": "Unauthorized",
                }
            return 200, {"ok": True, "result": script_updates}
        if "sendMessage" in url:
            return 200, {
                "ok": True,
                "result": {"message_id": 7, "chat": {"id": data.get("chat_id")}},
            }
        raise AssertionError(f"unexpected method in {url}")

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def test_end_to_end_poll_once_with_real_client(tmp_path: Path) -> None:
    jobs, gens = make_repos(tmp_path)
    seed_job(jobs, "dst1", "queued", caption="ship it")
    updates = [make_update(50, "123", "/queue")]
    transport = make_transport(updates)
    client = TelegramClient(FAKE_TOKEN, transport=transport)
    ctx = make_ctx(tmp_path, client, jobs, gens)
    store = OffsetStore(tmp_path / "offset.json")
    receiver = CommandReceiver(client, make_router(), ("123",), store)
    sent = receiver.poll_once(ctx)
    assert len(sent) == 1
    assert sent[0][0] == "123" and "dst1" in sent[0][1]
    assert store.get() == 51
    get_call = transport.calls[0]
    assert get_call["data"] == {"timeout": 30}
    # Long-poll rule: HTTP timeout exceeds the poll window.
    assert get_call["timeout"] >= 40
    send_call = transport.calls[1]
    assert send_call["data"]["chat_id"] == "123"
    assert FAKE_TOKEN not in send_call["data"]["text"]


def test_end_to_end_client_error_propagates_loudly(tmp_path: Path) -> None:
    transport = make_transport(None, status=401)
    client = TelegramClient(FAKE_TOKEN, transport=transport)
    jobs, gens = make_repos(tmp_path)
    ctx = make_ctx(tmp_path, client, jobs, gens)
    store = OffsetStore(tmp_path / "offset.json")
    receiver = CommandReceiver(client, make_router(), ("123",), store)
    with pytest.raises(AuthenticationError):
        receiver.poll_once(ctx)
    assert store.get() is None  # failed poll persists nothing


def test_receiver_constructor_validates_collaborators(tmp_path: Path) -> None:
    router = make_router()
    store = OffsetStore(tmp_path / "offset.json")
    with pytest.raises(ValueError):
        CommandReceiver(object(), router, ("123",), store)
    with pytest.raises(ValueError):
        CommandReceiver(FakeClient(), object(), ("123",), store)
    with pytest.raises(ValueError):
        CommandReceiver(FakeClient(), router, ("123",), object())
    with pytest.raises(ValueError):
        CommandReceiver(FakeClient(), router, 42, store)  # type: ignore[arg-type]
