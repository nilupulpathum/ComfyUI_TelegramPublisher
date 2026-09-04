"""T053/T054 tests: HTTP route adapters (publisher_nodes.routes).

No aiohttp import here (it is not installed in this dev env -- only inside
ComfyUI). ``register_routes`` is exercised with a FakeRoutes recorder that
implements the decorator-call style (``routes.get(path)(handler)``), the
same style ComfyUI's ``RouteTableDef`` uses. Logic functions are tested
directly with tmp-path stores. No network, no real tokens (``TEST_TOKEN``
fakes; one token-SHAPED synthetic string only to prove redaction).
"""

from __future__ import annotations

import json
import socket
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from publisher_nodes.routes import (
    ApiDeps,
    api_accounts,
    api_destinations,
    api_jobs,
    api_queue_status,
    api_test_connection,
    register_routes,
)
from services.queue import QueueStatus
from storage.config import Account, ConfigStore, Destination, FileSecretStore
from storage.database import Database
from storage.repositories import PublishJob, PublishJobRepository

FAKE_TOKEN = "TEST_TOKEN_xxx"
# Synthetic token-SHAPED string (matches the redact regex) -- fake, but
# proves nothing token-looking can leak through any envelope.
SHAPED_TOKEN = "123456:" + "A" * 35
ACCOUNT_ID = "acc1"
DEST_ID = "dst1"
CHAT_ID = "-100123"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("tests must not make real network calls")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


def make_stores(tmp_path: Path, token: str = FAKE_TOKEN):
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id=ACCOUNT_ID, name="Main bot"))
    config.add_destination(
        Destination(
            id=DEST_ID, name="Channel", account_id=ACCOUNT_ID, chat_id=CHAT_ID
        )
    )
    secrets.set_token(ACCOUNT_ID, token)
    return config, secrets


def make_repo(tmp_path: Path) -> PublishJobRepository:
    db = Database(tmp_path / "history.sqlite3")
    db.init_schema()
    return PublishJobRepository(db)


class FakeRoutes:
    """Decorator-style routes recorder (RouteTableDef convention)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, path: str):
        def decorator(handler):
            self.calls.append(("GET", path, handler))
            return handler

        return decorator

    def post(self, path: str):
        def decorator(handler):
            self.calls.append(("POST", path, handler))
            return handler

        return decorator


# ---------------------------------------------------------------------------
# register_routes (no aiohttp needed: registration itself is lazy-import-free)
# ---------------------------------------------------------------------------


def test_register_routes_records_expected_paths(tmp_path: Path):
    routes = FakeRoutes()
    deps = ApiDeps(
        config_path=tmp_path / "config.json",
        secret_path=tmp_path / "secrets.json",
        db_path=tmp_path / "history.sqlite3",
    )
    register_routes(routes, deps)
    recorded = {(method, path) for method, path, _ in routes.calls}
    assert ("GET", "/telegram_publisher/accounts") in recorded
    assert ("GET", "/telegram_publisher/destinations") in recorded
    assert ("POST", "/telegram_publisher/test_connection") in recorded
    assert ("GET", "/telegram_publisher/queue_status") in recorded
    assert ("GET", "/telegram_publisher/jobs") in recorded
    for _, _, handler in routes.calls:
        assert callable(handler)


def test_routes_module_imports_without_aiohttp():
    import sys

    assert "aiohttp" not in sys.modules


# ---------------------------------------------------------------------------
# api_accounts / api_destinations
# ---------------------------------------------------------------------------


def test_api_accounts_lists_ids_and_token_flags(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    body = api_accounts(config, secrets)
    assert body["ok"] is True
    assert body["accounts"] == [
        {"id": ACCOUNT_ID, "name": "Main bot", "has_token": True}
    ]
    assert FAKE_TOKEN not in json.dumps(body)


def test_api_accounts_missing_token_flags_false(tmp_path: Path):
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id=ACCOUNT_ID, name="Main bot"))
    body = api_accounts(config, secrets)
    assert body["ok"] is True
    assert body["accounts"][0]["has_token"] is False


def test_api_destinations_all_and_filtered(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    config.add_account(Account(id="acc2", name="Second"))
    config.add_destination(
        Destination(id="dst2", name="Other", account_id="acc2", chat_id="@other")
    )
    body = api_destinations(config)
    assert body["ok"] is True
    assert sorted(d["id"] for d in body["destinations"]) == ["dst1", "dst2"]
    filtered = api_destinations(config, ACCOUNT_ID)
    assert filtered["ok"] is True
    assert [d["id"] for d in filtered["destinations"]] == [DEST_ID]
    assert filtered["destinations"][0]["account_id"] == ACCOUNT_ID


def test_api_destinations_unknown_account_is_error_envelope(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    body = api_destinations(config, "ghost-acc")
    assert body["ok"] is False
    assert "ghost-acc" in body["error"]
    assert FAKE_TOKEN not in json.dumps(body)


def test_api_destinations_bad_account_type_is_error_envelope(tmp_path: Path):
    config, _ = make_stores(tmp_path)
    body = api_destinations(config, 123)  # type: ignore[arg-type]
    assert body["ok"] is False
    assert body["error"]


# ---------------------------------------------------------------------------
# api_test_connection (fake factory; ok + fail)
# ---------------------------------------------------------------------------


class _FakeBotInfo:
    def __init__(self, username: str | None = "testbot") -> None:
        self.username = username


class _OkClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def validate_token(self):
        assert self.token == FAKE_TOKEN
        return _FakeBotInfo("testbot")


class _FailClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def validate_token(self):
        raise ValueError("invalid token: unauthorized (401)")


def test_api_test_connection_ok(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    body = api_test_connection(
        config, secrets, _OkClient, {"account_id": ACCOUNT_ID}
    )
    assert body["ok"] is True
    assert body["bot_username"] == "testbot"
    assert FAKE_TOKEN not in json.dumps(body)


def test_api_test_connection_fail_is_envelope_without_token(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    body = api_test_connection(
        config, secrets, _FailClient, {"account_id": ACCOUNT_ID}
    )
    assert body["ok"] is False
    assert body["error"]
    assert FAKE_TOKEN not in json.dumps(body)


def test_api_test_connection_unknown_account(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    body = api_test_connection(
        config, secrets, _OkClient, {"account_id": "ghost-acc"}
    )
    assert body["ok"] is False
    assert "ghost-acc" in body["error"]


def test_api_test_connection_missing_account_id(tmp_path: Path):
    config, secrets = make_stores(tmp_path)
    for payload in ({}, {"account_id": ""}, {"account_id": None}):
        body = api_test_connection(config, secrets, _OkClient, payload)
        assert body["ok"] is False
        assert "account_id" in body["error"]


def test_api_test_connection_error_never_leaks_token_shape(tmp_path: Path):
    config, secrets = make_stores(tmp_path, token=SHAPED_TOKEN)

    class _LeakyClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def validate_token(self):
            raise RuntimeError(f"boom with {self.token} inside")

    body = api_test_connection(
        config, secrets, _LeakyClient, {"account_id": ACCOUNT_ID}
    )
    assert body["ok"] is False
    assert SHAPED_TOKEN not in json.dumps(body)
    assert "***" in body["error"]


# ---------------------------------------------------------------------------
# api_queue_status / api_jobs
# ---------------------------------------------------------------------------


class _FakeQueue:
    def status(self):
        return QueueStatus(
            queued=2,
            sending=1,
            success=5,
            failed=0,
            worker_running=True,
            buffered=3,
            maxsize=100,
        )


def test_api_queue_status_disabled_without_queue():
    body = api_queue_status(None)
    assert body == {"ok": True, "enabled": False}


def test_api_queue_status_enabled_with_queue():
    body = api_queue_status(_FakeQueue())
    assert body["ok"] is True
    assert body["enabled"] is True
    assert body["queued"] == 2
    assert body["worker_running"] is True


def test_api_jobs_lists_recent_newest_first(tmp_path: Path):
    repo = make_repo(tmp_path)
    repo.create(PublishJob(destination_id=DEST_ID, status="success"))
    repo.create(PublishJob(destination_id=DEST_ID, status="failed"))
    body = api_jobs(repo, 10)
    assert body["ok"] is True
    assert len(body["jobs"]) == 2
    assert body["jobs"][0]["status"] == "failed"  # newest first
    assert body["jobs"][0]["destination_id"] == DEST_ID


def test_api_jobs_bad_limits_are_error_envelopes(tmp_path: Path):
    repo = make_repo(tmp_path)
    for bad in (0, -1, "abc", None, True, 2.5):
        body = api_jobs(repo, bad)
        assert body["ok"] is False, f"limit={bad!r} should fail"
        assert body["error"]
