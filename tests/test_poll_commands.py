"""T070 tests: Telegram Command Poller node (Epic 7 driver).

Fake bot clients only (``get_updates``/``send_message``/``validate_token``/
``send_photo``). No real network, no real tokens (``TEST_TOKEN_xxx``
throughout), no ComfyUI import. Covers registration, the node contract,
the fail-closed skip without admins (zero network calls), the happy path
(unknown-text + unauthorized silence, ``/status`` reply, offset advance),
an approve-through-poller end-to-end, the 401 error path (RuntimeError,
token-free), and offset-path sanitization.
"""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path
from typing import Any

import pytest

import publisher_nodes
from publisher_nodes.poll_commands import (
    SKIP_NO_ADMINS,
    TelegramCommandPoller,
    default_offset_path,
    sanitize_account_id,
)
from services.review import ReviewStore, review_dir_for
from storage.config import Account, BotSettings, ConfigStore, Destination, FileSecretStore
from storage.database import Database
from storage.repositories import PublishJob
from telegram.errors import AuthenticationError
from telegram.models import BotInfo, SendMessageResult, SendPhotoResult

FAKE_TOKEN = "TEST_TOKEN_xxx"
ACCOUNT_ID = "acc1"
DEST_ID = "dst1"
DEST_CHAT_ID = "-100999"
ADMIN_CHAT = "111"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("tests must not make real network calls")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBotClient:
    """Recording stand-in: scripted get_updates batches + sent replies."""

    def __init__(
        self,
        batches: list[list[dict]] | None = None,
        *,
        fail_get: BaseException | None = None,
    ) -> None:
        self._batches = [list(b) for b in (batches or [])]
        self.sent: list[tuple[str, str]] = []
        self.photos: list[dict] = []
        self.get_calls: list[dict] = []
        self.fail_get = fail_get

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int | float = 30,
        *,
        client_timeout: float | None = None,
    ) -> list[dict]:
        self.get_calls.append({"offset": offset, "timeout": timeout})
        if self.fail_get is not None:
            raise self.fail_get
        batch = self._batches.pop(0) if self._batches else []
        if offset is None:
            return list(batch)
        return [
            u
            for u in batch
            if isinstance(u, dict)
            and isinstance(u.get("update_id"), int)
            and not isinstance(u.get("update_id"), bool)
            and u["update_id"] >= offset
        ]

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
        caption: str | None = None,
        *,
        protect_content: bool = False,
        disable_notification: bool = False,
        timeout: float | None = None,
    ) -> SendPhotoResult:
        self.photos.append(
            {"chat_id": chat_id, "bytes": bytes(image_bytes), "filename": filename}
        )
        return SendPhotoResult(message_id=900, chat_id=chat_id)

    def validate_token(self, *, timeout: float | None = None) -> BotInfo:
        return BotInfo(id=1, username="fakebot", first_name="Fake")


def make_update(uid: int, chat: Any, text: Any) -> dict:
    return {
        "update_id": uid,
        "message": {
            "message_id": 1,
            "chat": {"id": chat, "type": "private"},
            "from": {"id": "999"},
            "text": text,
        },
    }


def make_node(
    tmp_path: Path,
    fake: FakeBotClient,
    *,
    admins: tuple[str, ...] = (ADMIN_CHAT,),
) -> tuple[TelegramCommandPoller, list[str]]:
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id=ACCOUNT_ID, name="Main bot"))
    config.add_destination(
        Destination(
            id=DEST_ID, name="Channel", account_id=ACCOUNT_ID, chat_id=DEST_CHAT_ID
        )
    )
    secrets.set_token(ACCOUNT_ID, FAKE_TOKEN)
    config.save_settings(BotSettings(admin_chat_ids=list(admins)))
    factory_calls: list[str] = []

    def factory(token: str) -> FakeBotClient:
        factory_calls.append(token)
        assert token == FAKE_TOKEN  # resolved from the secret store, not code
        return fake

    node = TelegramCommandPoller(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path / "history" / "publisher.sqlite3",
    )
    return node, factory_calls


# ---------------------------------------------------------------------------
# Registration + contract
# ---------------------------------------------------------------------------


def test_registered_under_expected_id_with_display_mapping():
    assert (
        publisher_nodes.NODE_CLASS_MAPPINGS["Telegram Command Poller"]
        is TelegramCommandPoller
    )
    assert (
        publisher_nodes.NODE_DISPLAY_NAME_MAPPINGS["Telegram Command Poller"]
        == "Telegram Command Poller"
    )


def test_node_contract_attributes_and_input_keys():
    assert TelegramCommandPoller.RETURN_TYPES == ("STRING",)
    assert TelegramCommandPoller.FUNCTION == "poll"
    assert TelegramCommandPoller.CATEGORY == "Telegram"
    assert TelegramCommandPoller.OUTPUT_NODE is True
    required = TelegramCommandPoller.INPUT_TYPES()["required"]
    assert list(required.keys()) == ["account", "poll_rounds", "poll_timeout"]
    account_spec, rounds_spec, timeout_spec = (
        required["account"],
        required["poll_rounds"],
        required["poll_timeout"],
    )
    # COMBO shape: a one-tuple holding the options list, "" first.
    assert isinstance(account_spec, tuple) and len(account_spec) == 1
    assert isinstance(account_spec[0], list) and account_spec[0][:1] == [""]
    assert rounds_spec == ("INT", {"default": 3, "min": 1, "max": 20})
    assert timeout_spec == ("INT", {"default": 10, "min": 1, "max": 30})


def test_account_combo_falls_back_when_config_unreadable(monkeypatch):
    import publisher_nodes.web_api as web_api

    def _boom(config):
        raise OSError("config disk gone")

    monkeypatch.setattr(web_api, "account_ids", _boom)
    required = TelegramCommandPoller.INPUT_TYPES()["required"]
    assert required["account"] == ([""],)


def test_entry_import_exposes_all_three_nodes():
    import importlib.util

    root = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("telegrampublisher_poller_entry", root)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "Telegram Send Image" in module.NODE_CLASS_MAPPINGS
    assert "Telegram Send Album" in module.NODE_CLASS_MAPPINGS
    assert "Telegram Command Poller" in module.NODE_CLASS_MAPPINGS


# ---------------------------------------------------------------------------
# Skip without admins: fail-closed, zero network calls
# ---------------------------------------------------------------------------


def test_skip_without_admins_makes_zero_network_calls(tmp_path: Path):
    fake = FakeBotClient(batches=[[make_update(1, ADMIN_CHAT, "/status")]])
    node, factory_calls = make_node(tmp_path, fake, admins=())
    (summary,) = node.poll(ACCOUNT_ID, 3, 1)
    assert summary == SKIP_NO_ADMINS
    assert "no admin chats configured" in summary
    assert isinstance(summary, str)
    assert factory_calls == []  # no client built
    assert fake.get_calls == []  # no getUpdates issued
    assert fake.sent == []


# ---------------------------------------------------------------------------
# Happy path: silence + /status reply + offset advance
# ---------------------------------------------------------------------------


def test_happy_path_status_reply_with_silence_and_offset(tmp_path: Path):
    batches = [
        [
            make_update(1, ADMIN_CHAT, "/status"),
            make_update(2, ADMIN_CHAT, "just chatting"),  # unknown text: silence
            make_update(3, "999", "/status"),  # unauthorized chat: silence
        ],
        [],
        [],
    ]
    fake = FakeBotClient(batches=batches)
    node, factory_calls = make_node(tmp_path, fake)
    (summary,) = node.poll(ACCOUNT_ID, 3, 1)
    assert summary == "1 replies sent"
    assert factory_calls == [FAKE_TOKEN]
    assert len(fake.get_calls) == 3
    assert all(call["timeout"] == 1 for call in fake.get_calls)
    assert len(fake.sent) == 1
    chat_id, reply = fake.sent[0]
    assert chat_id == ADMIN_CHAT
    assert "queue:" in reply and "worker=off" in reply
    offset_file = tmp_path / "history" / "offsets" / f"{ACCOUNT_ID}.json"
    assert offset_file.is_file()
    import json

    assert json.loads(offset_file.read_text(encoding="utf-8")) == {"offset": 4}


def test_empty_poll_returns_no_new_commands(tmp_path: Path):
    fake = FakeBotClient(batches=[[]])
    node, _ = make_node(tmp_path, fake)
    assert node.poll(ACCOUNT_ID, 1, 1) == ("no new commands",)
    assert fake.sent == []


# ---------------------------------------------------------------------------
# Approve through the poller end-to-end (compact)
# ---------------------------------------------------------------------------


def test_approve_through_poller_marks_success_and_replies(tmp_path: Path):
    db_path = tmp_path / "history" / "publisher.sqlite3"
    db = Database(db_path)
    db.init_schema()
    from storage.repositories import GenerationRepository, PublishJobRepository

    job_id = "jobApprove1"
    PublishJobRepository(db).create(
        PublishJob(
            id=job_id,
            destination_id=DEST_ID,
            status="pending_review",
            filename="review.png",
            caption="hi",
            attempts=0,
        )
    )
    GenerationRepository(db).create(
        __import__("storage.repositories", fromlist=["Generation"]).Generation(
            job_id=job_id
        )
    )
    ReviewStore(review_dir_for(db_path)).save(job_id, PNG_BYTES)

    fake = FakeBotClient(batches=[[make_update(10, ADMIN_CHAT, f"/approve {job_id}")]])
    node, _ = make_node(tmp_path, fake)
    (summary,) = node.poll(ACCOUNT_ID, 1, 1)
    assert summary == "1 replies sent"
    row = PublishJobRepository(Database(db_path)).get(job_id)
    assert row is not None and row.status == "success"
    assert row.telegram_message_id == "900"
    assert len(fake.photos) == 1
    assert fake.photos[0]["chat_id"] == DEST_CHAT_ID
    assert fake.photos[0]["bytes"] == PNG_BYTES
    assert len(fake.sent) == 1
    assert "published to Channel, message 900" in fake.sent[0][1]
    assert not ReviewStore(review_dir_for(db_path)).exists(job_id)


# ---------------------------------------------------------------------------
# Error path: client failures become token-free RuntimeError
# ---------------------------------------------------------------------------


def test_client_401_raises_token_free_runtime_error(tmp_path: Path):
    fake = FakeBotClient(
        fail_get=AuthenticationError("getUpdates: invalid bot token")
    )
    node, _ = make_node(tmp_path, fake)
    with pytest.raises(RuntimeError, match="Telegram polling failed") as excinfo:
        node.poll(ACCOUNT_ID, 1, 1)
    assert FAKE_TOKEN not in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, AuthenticationError)


def test_unknown_account_raises_actionable_token_free_error(tmp_path: Path):
    fake = FakeBotClient()
    node, factory_calls = make_node(tmp_path, fake)
    with pytest.raises(RuntimeError, match="Telegram polling failed"):
        node.poll("nope", 1, 1)
    assert FAKE_TOKEN not in str(factory_calls)
    assert fake.get_calls == []


def test_bad_rounds_rejected_loudly(tmp_path: Path):
    fake = FakeBotClient()
    node, _ = make_node(tmp_path, fake)
    with pytest.raises(ValueError, match="poll_rounds"):
        node.poll(ACCOUNT_ID, 0, 1)
    with pytest.raises(ValueError, match="poll_timeout"):
        node.poll(ACCOUNT_ID, 1, 31)
    assert fake.get_calls == []


# ---------------------------------------------------------------------------
# Offset path sanitization
# ---------------------------------------------------------------------------


def test_sanitize_account_id_maps_unsafe_chars():
    assert sanitize_account_id("acc1") == "acc1"
    assert sanitize_account_id("acc/evil..:x") == "acc_evil___x"
    assert sanitize_account_id("a b@c!") == "a_b_c_"
    assert sanitize_account_id("") == "_"
    assert sanitize_account_id("A-Z_a~9") == "A-Z_a_9"


def test_default_offset_path_scheme(tmp_path: Path):
    db_path = tmp_path / "history" / "publisher.sqlite3"
    assert default_offset_path(db_path, "acc1") == (
        tmp_path / "history" / "offsets" / "acc1.json"
    )
    assert default_offset_path(db_path, "a/b").name == "a_b.json"
    assert default_offset_path(db_path, "a/b").parent.name == "offsets"
