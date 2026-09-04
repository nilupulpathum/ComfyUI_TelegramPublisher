"""T009 tests: Telegram Send Image node. Fake transport only.

No real network, no real tokens (``TEST_TOKEN_xxx`` throughout), no
ComfyUI import. Covers registration, happy path, failure mapping,
skip_duplicate refusal, empty batches, and unknown ids.
"""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import publisher_nodes
from publisher_nodes.send_image import TelegramSendImage
from storage.config import Account, ConfigStore, Destination, FileSecretStore
from telegram.client import TelegramClient
from telegram.errors import ConfigurationError

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


def ok_transport(record: list) -> Any:
    def fake(url, *, files, data, timeout):
        assert url.startswith("https://api.telegram.org/bot")
        record.append({"url": url, "files": files, "data": data})
        return 200, {"ok": True, "result": {"message_id": 42, "chat": {"id": -100123}}}

    return fake


def make_node(tmp_path: Path, transport: Any) -> TelegramSendImage:
    config, secrets = make_stores(tmp_path)

    def factory(token: str) -> TelegramClient:
        assert token == FAKE_TOKEN  # resolved from the secret store, not code
        return TelegramClient(token, transport=transport)

    return TelegramSendImage(
        config_store=config, secret_store=secrets, client_factory=factory
    )


def frame(h: int = 8, w: int = 8) -> np.ndarray:
    rng = np.random.default_rng(7)
    return (rng.random((h, w, 3))).astype(np.float32)


# ---------------------------------------------------------------------------
# Registration + contract
# ---------------------------------------------------------------------------


def test_registered_under_expected_id_with_display_mapping():
    assert publisher_nodes.NODE_CLASS_MAPPINGS["Telegram Send Image"] is TelegramSendImage
    assert publisher_nodes.NODE_DISPLAY_NAME_MAPPINGS["Telegram Send Image"] == (
        "Telegram Send Image"
    )


def test_node_contract_attributes_and_input_keys():
    assert TelegramSendImage.RETURN_TYPES == ("IMAGE",)
    assert TelegramSendImage.FUNCTION == "publish"
    assert TelegramSendImage.CATEGORY == "Telegram"
    assert TelegramSendImage.OUTPUT_NODE is True
    required = TelegramSendImage.INPUT_TYPES()["required"]
    assert list(required.keys()) == [
        "image",
        "account",
        "destination",
        "caption",
        "format",
        "quality",
        "protect_content",
        "disable_notification",
        "skip_duplicate",
        "wait_for_upload",
    ]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_happy_path_batch_returns_identical_object(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_transport(record))
    batch = np.stack([frame(), frame()])  # (2, 8, 8, 3)
    before = batch.copy()

    (result,) = node.publish(
        batch,
        ACCOUNT_ID,
        DEST_ID,
        caption="hello",
        format="png",
        quality=90,
        protect_content=True,
        disable_notification=False,
    )

    assert result is batch  # FR-003: original object, untouched
    assert np.array_equal(batch, before)
    sent = record[0]
    assert sent["data"]["chat_id"] == CHAT_ID
    assert sent["data"]["caption"] == "hello"
    assert sent["data"]["protect_content"] == "true"
    assert sent["data"]["disable_notification"] == "false"
    filename, payload, content_type = sent["files"]["photo"]
    assert filename == "comfyui_8x8.png"
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert content_type == "image/png"


def test_happy_path_single_frame_and_jpeg(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_transport(record))
    single = frame(6, 4)

    (result,) = node.publish(
        single, ACCOUNT_ID, DEST_ID, caption="", format="jpeg", quality=80
    )

    assert result is single
    sent = record[0]
    assert "caption" not in sent["data"]  # "" caption means no caption
    filename, payload, content_type = sent["files"]["photo"]
    assert filename == "comfyui_4x6.jpeg"
    assert payload[:2] == b"\xff\xd8"
    assert content_type == "image/jpeg"


# ---------------------------------------------------------------------------
# Failures: RuntimeError with actionable, token-free messages
# ---------------------------------------------------------------------------


def test_failed_upload_raises_runtime_error_without_token(tmp_path: Path):
    def bad_chat(url, *, files, data, timeout):
        return 400, {"ok": False, "error_code": 400, "description": "bad chat"}

    node = make_node(tmp_path, bad_chat)
    batch = np.stack([frame()])

    with pytest.raises(RuntimeError) as exc:
        node.publish(batch, ACCOUNT_ID, DEST_ID)

    message = str(exc.value)
    assert "publish" in message or "sendPhoto" in message  # actionable
    assert FAKE_TOKEN not in message
    assert FAKE_TOKEN not in repr(exc.value)


def test_failed_upload_leaves_tensor_intact(tmp_path: Path):
    def boom(url, *, files, data, timeout):
        raise TimeoutError("timed out")

    node = make_node(tmp_path, boom)
    batch = np.stack([frame()])
    before = batch.copy()

    with pytest.raises(RuntimeError):
        node.publish(batch, ACCOUNT_ID, DEST_ID)

    assert np.array_equal(batch, before)  # FR-004


def test_skip_duplicate_refuses_loudly_without_network(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_transport(record))

    with pytest.raises(ConfigurationError, match="T034"):
        node.publish(
            np.stack([frame()]), ACCOUNT_ID, DEST_ID, skip_duplicate=True
        )

    assert record == []


def test_empty_batch_rejected(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_transport(record))
    empty = np.zeros((0, 8, 8, 3), dtype=np.float32)

    with pytest.raises(RuntimeError, match="empty"):
        node.publish(empty, ACCOUNT_ID, DEST_ID)

    assert record == []


def test_unknown_destination_id_raises_actionable_error(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_transport(record))

    with pytest.raises(RuntimeError, match="ghost-dst"):
        node.publish(np.stack([frame()]), ACCOUNT_ID, "ghost-dst")

    assert record == []


def test_unknown_account_id_raises_actionable_error(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_transport(record))

    with pytest.raises(RuntimeError, match="ghost-acc"):
        node.publish(np.stack([frame()]), "ghost-acc", DEST_ID)

    assert record == []
