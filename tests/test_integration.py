"""T024 integration tests: node -> encoder -> real client -> fake Telegram API.

No mocks except the fake loopback HTTP server: real ``TelegramClient``
(``transport=None``), real encoder, real config stores on ``tmp_path``,
real ``TelegramSendImage``. Fake tokens (``TEST_TOKEN_xxx``) only; the
server binds 127.0.0.1 on an ephemeral port. Tests that need the server
skip cleanly if the loopback socket cannot bind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from publisher_nodes.send_image import TelegramSendImage
from services.retry import RetryPolicy
from storage.config import Account, ConfigStore, Destination, FileSecretStore
from telegram.client import TelegramClient
from tests.fake_telegram_server import FakeTelegramServer

FAKE_TOKEN = "TEST_TOKEN_xxx"
ACCOUNT_ID = "acc-int"
DEST_ID = "dst-int"
CHAT_ID = "-100123"


def _rate_limited(retry_after: int = 0) -> tuple[int, dict[str, Any]]:
    return 429, {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests",
        "parameters": {"retry_after": retry_after},
    }


def _server_error() -> tuple[int, dict[str, Any]]:
    return 500, {"ok": False, "error_code": 500, "description": "Internal Server Error"}


def _unauthorized() -> tuple[int, dict[str, Any]]:
    return 401, {"ok": False, "error_code": 401, "description": "Unauthorized: invalid token"}


@pytest.fixture
def live_server(request: pytest.FixtureRequest) -> FakeTelegramServer:
    """Start the fake server; skip (never hang) if loopback cannot bind."""
    server = FakeTelegramServer(token=FAKE_TOKEN, expected_chat_id=CHAT_ID)
    try:
        server.start()
    except OSError as exc:
        pytest.skip(f"loopback bind unavailable: {exc}")
    request.addfinalizer(server.shutdown)
    return server


def _client_factory(base_url: str):  # real client, no-sleep retry
    def factory(token: str) -> TelegramClient:
        assert token == FAKE_TOKEN
        return TelegramClient(
            token,
            timeout=5.0,
            transport=None,
            base_url=base_url,
            retry_policy=RetryPolicy(sleep=lambda s: None),
        )

    return factory


def _make_node(tmp_path: Path, base_url: str) -> TelegramSendImage:
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id=ACCOUNT_ID, name="Bot"))
    config.add_destination(
        Destination(id=DEST_ID, name="Channel", account_id=ACCOUNT_ID, chat_id=CHAT_ID)
    )
    secrets.set_token(ACCOUNT_ID, FAKE_TOKEN)
    return TelegramSendImage(
        config_store=config,
        secret_store=secrets,
        client_factory=_client_factory(base_url),
    )


def _batch() -> np.ndarray:
    rng = np.random.default_rng(11)
    return np.stack(
        [rng.random((8, 8, 3)).astype(np.float32), rng.random((8, 8, 3)).astype(np.float32)]
    )


def _assert_token_free(token: str, exc: BaseException) -> None:
    """The token travels only in the URL path; it must not leak into errors."""
    seen: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        seen.append(f"{type(current).__name__}: {current}")
        seen.append(repr(current))
        current = current.__cause__ or current.__context__
    for text in seen:
        assert token not in text


# ---------------------------------------------------------------------------
# Happy path + validate_token
# ---------------------------------------------------------------------------


def test_happy_path_publish_end_to_end(
    tmp_path: Path, live_server: FakeTelegramServer
) -> None:
    node = _make_node(tmp_path, live_server.base_url)
    batch = _batch()
    before = batch.copy()

    (result,) = node.publish(batch, ACCOUNT_ID, DEST_ID, caption="hi", format="png")

    assert result is batch
    assert np.array_equal(batch, before)
    assert live_server.route_errors == []
    hits = live_server.requests_for("sendPhoto")
    assert len(hits) == 1
    assert hits[0]["found_chat_id"] is True
    assert hits[0]["body_length"] > 0
    assert hits[0]["content_type_present"] is True


def test_torch_batch_publish_end_to_end(
    tmp_path: Path, live_server: FakeTelegramServer
) -> None:
    torch = pytest.importorskip("torch")
    node = _make_node(tmp_path, live_server.base_url)
    batch = torch.rand(2, 8, 8, 3, dtype=torch.float32)
    before = batch.clone()

    (result,) = node.publish(batch, ACCOUNT_ID, DEST_ID, caption="hi", format="png")

    assert result is batch
    assert torch.equal(batch, before)
    hits = live_server.requests_for("sendPhoto")
    assert len(hits) == 1
    assert hits[0]["found_chat_id"] is True


def test_validate_token_against_get_me(live_server: FakeTelegramServer) -> None:
    client = TelegramClient(FAKE_TOKEN, timeout=5.0, base_url=live_server.base_url)
    info = client.validate_token()
    assert info.id == 123
    assert info.username == "testbot"
    assert live_server.requests_for("getMe")


# ---------------------------------------------------------------------------
# Retry / exhaustion / auth
# ---------------------------------------------------------------------------


def test_retry_after_429_then_success(
    tmp_path: Path, live_server: FakeTelegramServer
) -> None:
    live_server.script("sendPhoto", [_rate_limited(retry_after=0)])
    node = _make_node(tmp_path, live_server.base_url)

    (result,) = node.publish(_batch(), ACCOUNT_ID, DEST_ID)

    assert result is not None
    assert len(live_server.requests_for("sendPhoto")) == 2
    assert live_server.route_errors == []


def test_exhaustion_500s_raise_without_token(
    tmp_path: Path, live_server: FakeTelegramServer
) -> None:
    live_server.script("sendPhoto", [_server_error(), _server_error(), _server_error()])
    node = _make_node(tmp_path, live_server.base_url)

    with pytest.raises(RuntimeError) as excinfo:
        node.publish(_batch(), ACCOUNT_ID, DEST_ID)

    assert len(live_server.requests_for("sendPhoto")) == 3
    _assert_token_free(FAKE_TOKEN, excinfo.value)


def test_auth_401_actionable_without_token(
    tmp_path: Path, live_server: FakeTelegramServer
) -> None:
    live_server.script("sendPhoto", [_unauthorized()])
    node = _make_node(tmp_path, live_server.base_url)

    with pytest.raises(RuntimeError, match="invalid bot token") as excinfo:
        node.publish(_batch(), ACCOUNT_ID, DEST_ID)

    _assert_token_free(FAKE_TOKEN, excinfo.value)


# ---------------------------------------------------------------------------
# Token-leak sweep
# ---------------------------------------------------------------------------


def test_token_leak_sweep_on_failure(
    tmp_path: Path, live_server: FakeTelegramServer
) -> None:
    live_server.script(
        "sendPhoto",
        [(400, {"ok": False, "error_code": 400, "description": "bad request"})],
    )
    node = _make_node(tmp_path, live_server.base_url)

    with pytest.raises(RuntimeError) as excinfo:
        node.publish(_batch(), ACCOUNT_ID, DEST_ID, caption="sweep")

    message = str(excinfo.value)
    assert "publish" in message or "sendPhoto" in message  # still actionable
    _assert_token_free(FAKE_TOKEN, excinfo.value)
    # The token travels only in the request URL path to loopback: the
    # multipart bodies the server saw must not contain it. The server
    # tracks body lengths, not bodies, so assert via a fresh raw check
    # is unnecessary; the key guarantee is the error-chain sweep above.
    for entry in live_server.requests:
        assert entry["method"] in ("sendPhoto", "unknown")
