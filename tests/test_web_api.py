"""Epic 5 server half: tests for publisher_nodes/web_api.py (T051-T054).

No network (fake client_factory), no real tokens (TEST_TOKEN_xxx only).
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from publisher_nodes import web_api
from publisher_nodes.web_api import (
    AccountInfo,
    ConnectionResult,
    DestinationInfo,
    account_ids,
    destination_ids,
    list_accounts,
    list_destinations,
    queue_status,
    recent_jobs,
)
from services.queue import PublishPayload, PublishQueue
from storage.config import Account, ConfigStore, Destination, FileSecretStore
from storage.database import Database
from storage.repositories import PublishJob, PublishJobRepository
from telegram.errors import AuthenticationError, TransientTelegramError
from telegram.models import BotInfo

TOKEN_A = "TEST_TOKEN_aaa"
TOKEN_B = "TEST_TOKEN_bbb"


def make_stores(tmp_path: Path) -> tuple[ConfigStore, FileSecretStore]:
    config = ConfigStore(tmp_path / "accounts.json")
    secrets = FileSecretStore(tmp_path / "tokens.json")
    config.add_account(Account(id="acc-a", name="Main bot"))
    config.add_account(Account(id="acc-b", name="Second bot"))
    config.add_destination(
        Destination(id="dest-1", name="Channel", account_id="acc-a", chat_id="-1001")
    )
    config.add_destination(
        Destination(id="dest-2", name="Group", account_id="acc-a", chat_id="-1002")
    )
    config.add_destination(
        Destination(id="dest-3", name="Other", account_id="acc-b", chat_id="-1003")
    )
    secrets.set_token("acc-a", TOKEN_A)
    return config, secrets


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "history.sqlite3")
    db.init_schema()
    return db


class _OkClient:
    def __init__(self, seen: dict, token: str) -> None:
        seen["token"] = token

    def validate_token(self) -> BotInfo:
        return BotInfo(id=1, username="testbot", first_name="Test")


class _AuthClient:
    def __init__(self, token: str) -> None:
        pass

    def validate_token(self) -> BotInfo:
        raise AuthenticationError("getMe: invalid bot token")


class _TransientClient:
    def __init__(self, token: str) -> None:
        pass

    def validate_token(self) -> BotInfo:
        raise TransientTelegramError("getMe: boom")


# ---------------------------------------------------------------------------
# Accounts / destinations
# ---------------------------------------------------------------------------


def test_list_accounts_has_token_true_false(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    infos = list_accounts(config, secrets)
    by_id = {i.id: i for i in infos}
    assert isinstance(infos[0], AccountInfo)
    assert by_id["acc-a"] == AccountInfo(id="acc-a", name="Main bot", has_token=True)
    assert by_id["acc-b"] == AccountInfo(id="acc-b", name="Second bot", has_token=False)
    assert TOKEN_A not in str(infos)
    assert TOKEN_A not in str([asdict(i) for i in infos])


def test_list_accounts_empty(tmp_path: Path) -> None:
    config = ConfigStore(tmp_path / "accounts.json")
    secrets = FileSecretStore(tmp_path / "tokens.json")
    assert list_accounts(config, secrets) == []


def test_list_destinations_all_and_filtered(tmp_path: Path) -> None:
    config, _ = make_stores(tmp_path)
    all_dests = list_destinations(config)
    assert {d.id for d in all_dests} == {"dest-1", "dest-2", "dest-3"}
    assert all(isinstance(d, DestinationInfo) for d in all_dests)
    filtered = list_destinations(config, "acc-a")
    assert sorted(d.id for d in filtered) == ["dest-1", "dest-2"]
    assert all(d.account_id == "acc-a" for d in filtered)


def test_list_destinations_hides_chat_id(tmp_path: Path) -> None:
    config, _ = make_stores(tmp_path)
    for dest in list_destinations(config):
        assert not hasattr(dest, "chat_id")
        assert "-1001" not in str(asdict(dest))
    assert "-1001" not in str(list_destinations(config))


def test_list_destinations_unknown_filter_raises_valueerror(tmp_path: Path) -> None:
    config, _ = make_stores(tmp_path)
    with pytest.raises(ValueError):
        list_destinations(config, "nope")
    with pytest.raises(ValueError):
        list_destinations(config, "")


def test_account_ids_sorted(tmp_path: Path) -> None:
    config, _ = make_stores(tmp_path)
    assert account_ids(config) == ["acc-a", "acc-b"]


def test_destination_ids_sorted_and_filtered(tmp_path: Path) -> None:
    config, _ = make_stores(tmp_path)
    assert destination_ids(config) == ["dest-1", "dest-2", "dest-3"]
    assert destination_ids(config, "acc-b") == ["dest-3"]
    with pytest.raises(ValueError):
        destination_ids(config, "nope")


def test_keyerror_mapped_to_valueerror_not_keyerror(tmp_path: Path) -> None:
    config, _ = make_stores(tmp_path)
    with pytest.raises(ValueError):
        list_destinations(config, "ghost")
    try:
        list_destinations(config, "ghost")
    except KeyError:
        pytest.fail("KeyError must be mapped to ValueError")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


def test_connection_success(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    seen: dict = {}
    result = web_api.test_connection(
        config, secrets, lambda token: _OkClient(seen, token), "acc-a"
    )
    assert isinstance(result, ConnectionResult)
    assert result.ok is True
    assert result.bot_username == "testbot"
    assert result.error is None
    assert seen["token"] == TOKEN_A
    assert TOKEN_A not in str(result)


def test_connection_success_with_destination(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    result = web_api.test_connection(
        config, secrets, lambda token: _OkClient({}, token), "acc-a", "dest-1"
    )
    assert result.ok is True


def test_connection_auth_failure(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    result = web_api.test_connection(config, secrets, _AuthClient, "acc-a")
    assert result.ok is False
    assert result.bot_username is None
    assert "invalid" in result.error.lower()
    assert TOKEN_A not in result.error


def test_connection_transient_failure(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    result = web_api.test_connection(config, secrets, _TransientClient, "acc-a")
    assert result.ok is False
    assert "reachable but request failed" in result.error


def test_connection_unknown_account_never_raises(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    result = web_api.test_connection(config, secrets, _AuthClient, "ghost")
    assert result.ok is False
    assert "unknown account" in result.error.lower()


def test_connection_unknown_destination(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    result = web_api.test_connection(
        config, secrets, lambda token: _OkClient({}, token), "acc-a", "ghost"
    )
    assert result.ok is False
    assert "unknown destination" in result.error.lower()


def test_connection_account_mismatch(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    # dest-3 belongs to acc-b, tested with acc-a.
    result = web_api.test_connection(
        config, secrets, lambda token: _OkClient({}, token), "acc-a", "dest-3"
    )
    assert result.ok is False
    assert "acc-b" in result.error


def test_connection_missing_token(tmp_path: Path) -> None:
    config, secrets = make_stores(tmp_path)
    result = web_api.test_connection(
        config, secrets, lambda token: _OkClient({}, token), "acc-b"
    )
    assert result.ok is False
    assert "secrets/tokens.json" in result.error
    assert TOKEN_A not in result.error and TOKEN_B not in result.error


# ---------------------------------------------------------------------------
# Queue status / recent jobs
# ---------------------------------------------------------------------------


def test_queue_status_none() -> None:
    assert queue_status(None) == {"enabled": False}


def test_queue_status_live(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    queue = PublishQueue(db, sender=lambda payload: "1")
    status = queue_status(queue)
    assert status["enabled"] is True
    for key in ("queued", "sending", "success", "failed", "worker_running",
                "buffered", "maxsize"):
        assert key in status
    queue.enqueue(
        PublishPayload(
            kind="photo",
            account_id="acc-a",
            destination_id="dest-1",
            chat_id="-1001",
            files=((b"bytes", "a.png"),),
        )
    )
    status2 = queue_status(queue)
    assert status2["queued"] == 1
    assert status2["buffered"] == 1


def test_recent_jobs_shape(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    repo = PublishJobRepository(db)
    repo.create(
        PublishJob(
            destination_id="dest-1",
            status="success",
            caption="hello",
            telegram_message_id="42",
        )
    )
    rows = recent_jobs(repo)
    assert len(rows) == 1
    row = rows[0]
    for col in ("id", "destination_id", "status", "image_hash", "filename",
                "caption", "attempts", "telegram_message_id", "error_code",
                "error_message", "next_retry_at", "created_at", "updated_at"):
        assert col in row
    assert row["caption"] == "hello"
    assert row["telegram_message_id"] == "42"
    import json

    json.dumps(rows)  # must be JSON-serializable


@pytest.mark.parametrize("bad", [0, -1, 101, 1000, "20", 2.5, True])
def test_recent_jobs_limit_validation(tmp_path: Path, bad) -> None:
    db = make_db(tmp_path)
    repo = PublishJobRepository(db)
    with pytest.raises(ValueError):
        recent_jobs(repo, bad)


def test_recent_jobs_limit_boundaries(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    repo = PublishJobRepository(db)
    assert recent_jobs(repo, 1) == []
    assert recent_jobs(repo, 100) == []
