"""T031 tests: Telegram Send Album node. Fake transport only.

No real network, no real tokens (``TEST_TOKEN_xxx`` throughout), no
ComfyUI import. Covers registration, contract, happy path (job +
generation rows, passthrough identity), 1-frame / 11-frame rejection,
template captions, whole-album duplicate refusal, failure history, and
token-free messages/logs.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import publisher_nodes
from publisher_nodes.send_album import TelegramSendAlbum
from services.dedup import sha256_hex
from services.encoder import encode_image
from storage.config import Account, ConfigStore, Destination, FileSecretStore
from storage.database import Database
from storage.repositories import PublishJobRepository, GenerationRepository
from telegram.client import TelegramClient

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


def ok_media_transport(record: list, ids: tuple[int, ...] = (11, 12)) -> Any:
    def fake(url, *, files, data, timeout):
        assert url.startswith("https://api.telegram.org/bot")
        record.append({"url": url, "files": files, "data": data})
        return 200, {
            "ok": True,
            "result": [{"message_id": mid} for mid in ids],
        }

    return fake


def make_node(
    tmp_path: Path, transport: Any, ids: tuple[int, ...] = (11, 12)
) -> TelegramSendAlbum:
    config, secrets = make_stores(tmp_path)

    def factory(token: str) -> TelegramClient:
        assert token == FAKE_TOKEN  # resolved from the secret store, not code
        return TelegramClient(token, transport=transport)

    return TelegramSendAlbum(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path / "history.sqlite3",
    )


def frame(seed: int = 7, h: int = 8, w: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random((h, w, 3)).astype(np.float32)


def repos_for(tmp_path: Path) -> tuple[PublishJobRepository, GenerationRepository]:
    db = Database(tmp_path / "history.sqlite3")
    db.init_schema()
    return PublishJobRepository(db), GenerationRepository(db)


# ---------------------------------------------------------------------------
# Registration + contract
# ---------------------------------------------------------------------------


def test_registered_under_expected_id_with_display_mapping():
    assert publisher_nodes.NODE_CLASS_MAPPINGS["Telegram Send Album"] is TelegramSendAlbum
    assert (
        publisher_nodes.NODE_DISPLAY_NAME_MAPPINGS["Telegram Send Album"]
        == "Telegram Send Album"
    )


def test_node_contract_attributes_and_input_keys():
    assert TelegramSendAlbum.RETURN_TYPES == ("IMAGE",)
    assert TelegramSendAlbum.FUNCTION == "publish"
    assert TelegramSendAlbum.CATEGORY == "Telegram"
    assert TelegramSendAlbum.OUTPUT_NODE is True
    required = TelegramSendAlbum.INPUT_TYPES()["required"]
    assert list(required.keys()) == [
        "images",
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
    assert required["caption"] == ("STRING", {"multiline": True, "default": ""})
    assert required["format"] == (["png", "jpeg"],)
    assert required["quality"] == ("INT", {"default": 90, "min": 1, "max": 100})
    # T051/T052: account/destination are COMBO selectors (1-tuple holding
    # the options list, "" first = explicit unset), not free STRINGs.
    for key in ("account", "destination"):
        spec = required[key]
        assert isinstance(spec, tuple) and len(spec) == 1
        assert isinstance(spec[0], list)
        assert spec[0] and spec[0][0] == ""
    optional = TelegramSendAlbum.INPUT_TYPES()["optional"]
    assert list(optional.keys()) == [
        "prompt",
        "negative_prompt",
        "seed",
        "steps",
        "cfg",
        "sampler",
        "scheduler",
        "model",
    ]


def test_album_shares_combo_helper_with_send_image():
    # T051/T052: the options builder is defined once in send_image and
    # reused by send_album (no duplicated logic).
    import publisher_nodes.send_album as album_mod
    import publisher_nodes.send_image as image_mod

    assert album_mod._combo_options is image_mod._combo_options
    assert album_mod._account_options is image_mod._account_options
    assert album_mod._destination_options is image_mod._destination_options


def test_entry_import_exposes_both_nodes():
    import importlib.util

    root = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("telegrampublisher_album_entry", root)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "Telegram Send Image" in module.NODE_CLASS_MAPPINGS
    assert "Telegram Send Album" in module.NODE_CLASS_MAPPINGS


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_two_frame_happy_path_writes_history_and_passes_through(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_media_transport(record))
    batch = np.stack([frame(7), frame(8)])
    before = batch.copy()

    (result,) = node.publish(
        batch, ACCOUNT_ID, DEST_ID, caption="hello", prompt="a cat"
    )

    assert result is batch  # FR-003: original object, untouched
    assert np.array_equal(batch, before)

    sent = record[0]
    assert sent["data"]["chat_id"] == CHAT_ID
    media = json.loads(sent["data"]["media"])
    assert len(media) == 2
    assert media[0]["caption"] == "hello"  # caption on first item only
    assert "caption" not in media[1]
    assert sorted(sent["files"].keys()) == ["photo0", "photo1"]
    for key in ("photo0", "photo1"):
        filename, payload, _ = sent["files"][key]
        assert filename == "comfyui_8x8.png"
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"

    job_repo, gen_repo = repos_for(tmp_path)
    jobs = job_repo.list_recent()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == "success"
    assert job.destination_id == DEST_ID
    assert job.telegram_message_id == "11,12"
    assert job.filename == "comfyui_8x8.png"
    assert job.caption == "hello"
    assert job.attempts == 1
    expected_hash = sha256_hex(encode_image(batch[0], format="png").data)
    assert job.image_hash == expected_hash
    gens = gen_repo.list_for_job(str(job.id))
    assert len(gens) == 1
    assert gens[0].prompt == "a cat"
    assert gens[0].width == 8 and gens[0].height == 8


def test_template_caption_rendered_on_first_item(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_media_transport(record))
    batch = np.stack([frame(7), frame(9)])

    node.publish(
        batch, ACCOUNT_ID, DEST_ID, caption="{{prompt}}!", prompt="a cat"
    )

    media = json.loads(record[0]["data"]["media"])
    assert media[0]["caption"] == "a cat!"
    job_repo, _ = repos_for(tmp_path)
    assert job_repo.list_recent()[0].caption == "a cat!"


def test_unknown_caption_var_warns_but_sends(tmp_path: Path, caplog):
    record: list = []
    node = make_node(tmp_path, ok_media_transport(record))
    batch = np.stack([frame(7), frame(9)])

    with caplog.at_level(logging.WARNING):
        node.publish(batch, ACCOUNT_ID, DEST_ID, caption="x{{nope}}y")

    media = json.loads(record[0]["data"]["media"])
    assert media[0]["caption"] == "xy"
    assert any("nope" in rec.getMessage() for rec in caplog.records)
    assert FAKE_TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# Rejections: frame limits + duplicates + failures
# ---------------------------------------------------------------------------


def test_single_frame_rejected_without_network(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_media_transport(record))

    with pytest.raises(ValueError, match=r"2-10.*got 1"):
        node.publish(np.stack([frame(7)]), ACCOUNT_ID, DEST_ID)

    assert record == []


def test_eleven_frames_rejected_without_network(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_media_transport(record))
    batch = np.stack([frame(s) for s in range(11)])

    with pytest.raises(ValueError, match=r"2-10.*got 11"):
        node.publish(batch, ACCOUNT_ID, DEST_ID)

    assert record == []


def test_skip_duplicate_hit_refuses_whole_album(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_media_transport(record))
    batch = np.stack([frame(7), frame(8)])

    (result,) = node.publish(batch, ACCOUNT_ID, DEST_ID)
    assert result is batch
    assert len(record) == 1

    with pytest.raises(RuntimeError, match="[Dd]uplicate"):
        node.publish(batch, ACCOUNT_ID, DEST_ID, skip_duplicate=True)

    assert len(record) == 1  # refusal made no second network call


def test_skip_duplicate_miss_publishes(tmp_path: Path):
    record: list = []
    node = make_node(tmp_path, ok_media_transport(record))
    batch = np.stack([frame(7), frame(8)])

    (result,) = node.publish(batch, ACCOUNT_ID, DEST_ID, skip_duplicate=True)

    assert result is batch
    assert len(record) == 1


def test_failure_records_failed_row_then_raises_without_token(tmp_path: Path):
    def bad_chat(url, *, files, data, timeout):
        return 400, {"ok": False, "error_code": 400, "description": "bad chat"}

    node = make_node(tmp_path, bad_chat)
    batch = np.stack([frame(7), frame(8)])
    before = batch.copy()

    with pytest.raises(RuntimeError) as exc:
        node.publish(batch, ACCOUNT_ID, DEST_ID)

    message = str(exc.value)
    assert "publish" in message or "sendMediaGroup" in message
    assert FAKE_TOKEN not in message
    assert FAKE_TOKEN not in repr(exc.value)
    assert np.array_equal(batch, before)  # FR-004

    job_repo, _ = repos_for(tmp_path)
    jobs = job_repo.list_recent()
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    assert jobs[0].error_code
    assert FAKE_TOKEN not in (jobs[0].error_message or "")


def test_history_failure_still_publishes(tmp_path: Path):
    record: list = []
    config, secrets = make_stores(tmp_path)

    def factory(token: str) -> TelegramClient:
        assert token == FAKE_TOKEN
        return TelegramClient(token, transport=ok_media_transport(record))

    # A directory as db_path: SQLite cannot open it, so history warns
    # and the publish must still succeed.
    node = TelegramSendAlbum(
        config_store=config,
        secret_store=secrets,
        client_factory=factory,
        db_path=tmp_path,
    )
    batch = np.stack([frame(7), frame(8)])

    (result,) = node.publish(
        batch, ACCOUNT_ID, DEST_ID, skip_duplicate=True
    )

    assert result is batch
    assert len(record) == 1
