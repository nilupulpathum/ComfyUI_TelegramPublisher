"""T005/T006: TelegramClient via fake transport. Never touches the network.

All tokens are fakes (``TEST_TOKEN_xxx``). A session guard blocks
``urllib.request.urlopen`` so any accidental real-network attempt fails.
"""

import socket
import urllib.request

import pytest

from telegram.client import TelegramClient
from services.retry import RetryPolicy
from telegram.errors import (
    AuthenticationError,
    ConfigurationError,
    DestinationError,
    EncodingError,
    PermanentTelegramError,
    RateLimitError,
    TransientTelegramError,
)
from telegram.models import BotInfo, SendMessageResult, SendPhotoResult

FAKE_TOKEN = "TEST_TOKEN_xxx"
# Synthetic token-shaped value for leak assertions. Fake only.
LEAK_SHAPE = "999888:" + "B" * 35


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("tests must not make real network calls")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


def make_fake(status, payload, record=None):
    def fake(url, *, files, data, timeout):
        assert url.startswith("https://api.telegram.org/bot")
        assert FAKE_TOKEN in url  # token travels in URL path only
        if record is not None:
            record.append({"url": url, "files": files, "data": data, "timeout": timeout})
        return status, payload

    return fake


def test_validate_token_happy_path():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(
            200,
            {"ok": True, "result": {"id": 7, "username": "demo_bot", "first_name": "Demo"}},
        ),
    )
    info = client.validate_token()
    assert info == BotInfo(id=7, username="demo_bot", first_name="Demo")


def test_validate_token_invalid_raises_authentication():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
    )
    with pytest.raises(AuthenticationError):
        client.validate_token()


def test_empty_token_rejected():
    with pytest.raises(ConfigurationError):
        TelegramClient("   ")


def test_send_photo_happy_path():
    record: list = []
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(
            200,
            {"ok": True, "result": {"message_id": 11, "chat": {"id": -100123}}},
            record,
        ),
    )
    res = client.send_photo("-100123", b"\xff\xd8fake", "img.jpg", caption="hi")
    assert isinstance(res, SendPhotoResult)
    assert res.message_id == 11
    assert isinstance(res.chat_id, str)  # FR-002: string end-to-end
    assert res.chat_id == "-100123"
    sent = record[0]
    assert sent["data"]["chat_id"] == "-100123"
    assert sent["files"]["photo"][0] == "img.jpg"


def test_send_media_group_happy_path():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(
            200,
            {"ok": True, "result": [{"message_id": 1}, {"message_id": 2}]},
        ),
    )
    ids = client.send_media_group("-100123", [(b"a", "a.png"), (b"b", "b.png")])
    assert ids == [1, 2]


def test_send_message_happy_path():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(
            200, {"ok": True, "result": {"message_id": 5, "chat": {"id": "@chan"}}}
        ),
    )
    res = client.send_message("@chan", "hello")
    assert isinstance(res, SendMessageResult)
    assert (res.message_id, res.chat_id) == (5, "@chan")


def test_ok_false_without_code_is_permanent():
    client = TelegramClient(
        FAKE_TOKEN, transport=make_fake(200, {"ok": False, "description": "weird"})
    )
    with pytest.raises(PermanentTelegramError):
        client.send_message("@chan", "hi")


def test_malformed_json_like_payload_is_permanent():
    # Fake transport signalling a broken body the way the default
    # transport surfaces JSON decode failures.
    def fake(url, *, files, data, timeout):
        raise ValueError("No JSON could be decoded")

    with pytest.raises(PermanentTelegramError):
        TelegramClient(FAKE_TOKEN, transport=fake).validate_token()


def test_non_dict_payload_is_permanent():
    client = TelegramClient(FAKE_TOKEN, transport=make_fake(200, "not-a-dict"))
    with pytest.raises(PermanentTelegramError):
        client.validate_token()


def test_http_400_maps_to_destination():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(400, {"ok": False, "error_code": 400, "description": "bad chat"}),
    )
    with pytest.raises(DestinationError):
        client.send_message("bad", "hi")


def test_http_401_maps_to_authentication():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
    )
    with pytest.raises(AuthenticationError):
        client.send_message("@chan", "hi")


def test_http_403_maps_to_destination():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(403, {"ok": False, "error_code": 403, "description": "kicked"}),
    )
    with pytest.raises(DestinationError):
        client.send_photo("@chan", b"x", "a.png")


def test_http_429_carries_retry_after():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
        ),
    )
    with pytest.raises(RateLimitError) as exc:
        client.send_message("@chan", "hi")
    assert exc.value.retry_after == 17


def test_http_500_maps_to_transient():
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(500, {"ok": False, "error_code": 500, "description": "boom"}),
    )
    with pytest.raises(TransientTelegramError):
        client.send_message("@chan", "hi")


def test_timeout_maps_to_transient():
    def fake(url, *, files, data, timeout):
        raise TimeoutError("timed out")

    with pytest.raises(TransientTelegramError):
        TelegramClient(FAKE_TOKEN, transport=fake).send_message("@chan", "hi")


def test_connection_error_maps_to_transient():
    def fake(url, *, files, data, timeout):
        raise ConnectionError("reset")

    with pytest.raises(TransientTelegramError):
        TelegramClient(FAKE_TOKEN, transport=fake).validate_token()


def test_empty_chat_id_rejected_without_network():
    calls: list = []
    client = TelegramClient(FAKE_TOKEN, transport=make_fake(200, {}, calls))
    with pytest.raises(DestinationError):
        client.send_message("  ", "hi")
    assert calls == []


def test_empty_image_rejected():
    client = TelegramClient(FAKE_TOKEN, transport=make_fake(200, {}, []))
    with pytest.raises(EncodingError):
        client.send_photo("@chan", b"", "a.png")


def test_exceptions_never_carry_token_shape():
    token_like = LEAK_SHAPE
    client = TelegramClient(
        FAKE_TOKEN,
        transport=make_fake(
            400, {"ok": False, "error_code": 400, "description": f"chat {token_like} bad"}
        ),
    )
    try:
        client.send_message(token_like, "hi")
        raise AssertionError("expected DestinationError")
    except DestinationError as exc:
        assert token_like not in str(exc)
        assert token_like not in repr(exc)


# -- T020/T021/T022 additions: retry policy + per-call timeout ----------


def make_script(script, timeouts=None):
    """Counting fake transport replaying a script of (status, payload)."""
    calls: list = []
    state = {"n": 0}

    def fake(url, *, files, data, timeout):
        assert url.startswith("https://api.telegram.org/bot")
        calls.append({"files": files, "data": data, "timeout": timeout})
        if timeouts is not None:
            timeouts.append(timeout)
        item = script[min(state["n"], len(script) - 1)]
        state["n"] += 1
        return item

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def rate_limited(retry_after=None):
    payload: dict = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests",
    }
    if retry_after is not None:
        payload["parameters"] = {"retry_after": retry_after}
    return 429, payload


def test_default_client_does_not_retry():
    fake = make_script(
        [(500, {"ok": False, "error_code": 500, "description": "boom"})]
    )
    client = TelegramClient(FAKE_TOKEN, transport=fake)
    with pytest.raises(TransientTelegramError):
        client.send_message("@chan", "hi")
    assert len(fake.calls) == 1


def test_retry_429_then_success_on_third_call():
    fake = make_script(
        [
            rate_limited(1),
            rate_limited(),
            (200, {"ok": True, "result": {"message_id": 9, "chat": {"id": "@chan"}}}),
        ]
    )
    client = TelegramClient(
        FAKE_TOKEN,
        transport=fake,
        retry_policy=RetryPolicy(sleep=lambda s: None),
    )
    res = client.send_message("@chan", "hi")
    assert res.message_id == 9
    assert len(fake.calls) == 3


def test_retry_500_exhausts_and_raises_last_error():
    fake = make_script(
        [
            (500, {"ok": False, "error_code": 500, "description": "boom1"}),
            (500, {"ok": False, "error_code": 500, "description": "boom2"}),
            (500, {"ok": False, "error_code": 500, "description": "boom3"}),
        ]
    )
    client = TelegramClient(
        FAKE_TOKEN,
        transport=fake,
        retry_policy=RetryPolicy(sleep=lambda s: None),
    )
    with pytest.raises(TransientTelegramError) as info:
        client.send_message("@chan", "hi")
    assert "boom3" in str(info.value)
    assert len(fake.calls) == 3


def test_retry_401_raises_immediately_with_one_call():
    fake = make_script(
        [(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})]
    )
    client = TelegramClient(
        FAKE_TOKEN,
        transport=fake,
        retry_policy=RetryPolicy(sleep=lambda s: None),
    )
    with pytest.raises(AuthenticationError):
        client.send_message("@chan", "hi")
    assert len(fake.calls) == 1


def test_retry_after_honored_within_cap():
    sleeps: list = []
    fake = make_script(
        [
            rate_limited(5),
            (200, {"ok": True, "result": {"message_id": 1, "chat": {"id": "@c"}}}),
        ]
    )
    client = TelegramClient(
        FAKE_TOKEN,
        transport=fake,
        retry_policy=RetryPolicy(sleep=sleeps.append),
    )
    client.send_message("@c", "hi")
    assert len(sleeps) == 1
    assert sleeps[0] <= 5
    assert sleeps[0] == 5.0


def test_retry_without_retry_after_uses_backoff():
    sleeps: list = []
    fake = make_script(
        [
            rate_limited(),
            (200, {"ok": True, "result": {"message_id": 1, "chat": {"id": "@c"}}}),
        ]
    )
    client = TelegramClient(
        FAKE_TOKEN,
        transport=fake,
        retry_policy=RetryPolicy(
            base_delay=2.0, max_delay=100.0, jitter=0.0, sleep=sleeps.append
        ),
    )
    client.send_message("@c", "hi")
    assert sleeps == [2.0]


def test_retry_after_larger_than_max_delay_gets_capped():
    sleeps: list = []
    fake = make_script(
        [
            rate_limited(100),
            (200, {"ok": True, "result": {"message_id": 1, "chat": {"id": "@c"}}}),
        ]
    )
    client = TelegramClient(
        FAKE_TOKEN,
        transport=fake,
        retry_policy=RetryPolicy(max_delay=7.0, sleep=sleeps.append),
    )
    client.send_message("@c", "hi")
    assert sleeps == [7.0]


def test_per_call_timeout_override_reaches_transport():
    record: list = []
    client = TelegramClient(
        FAKE_TOKEN,
        timeout=30.0,
        transport=make_fake(
            200, {"ok": True, "result": {"message_id": 5, "chat": {"id": "@chan"}}},
            record,
        ),
    )
    client.send_message("@chan", "hi", timeout=5.0)
    assert record[0]["timeout"] == 5.0


def test_per_call_timeout_defaults_to_client_timeout():
    record: list = []
    client = TelegramClient(
        FAKE_TOKEN,
        timeout=12.0,
        transport=make_fake(
            200, {"ok": True, "result": {"message_id": 5, "chat": {"id": "@chan"}}},
            record,
        ),
    )
    client.send_message("@chan", "hi")
    assert record[0]["timeout"] == 12.0


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_invalid_timeout_raises_before_transport(bad):
    calls: list = []
    client = TelegramClient(FAKE_TOKEN, transport=make_fake(200, {}, calls))
    with pytest.raises(ValueError):
        client.send_message("@chan", "hi", timeout=bad)
    with pytest.raises(ValueError):
        client.validate_token(timeout=bad)
    with pytest.raises(ValueError):
        client.send_photo("@chan", b"x", "a.png", timeout=bad)
    with pytest.raises(ValueError):
        client.send_media_group("@chan", [(b"x", "a.png")], timeout=bad)
    assert calls == []
