"""T055 tests: telegram/friendly.py user-friendly error mapping.

No network, no real tokens. The synthetic token shape below
(``digits:35-char-secret``) is fake and only exercises the redact backstop.
"""

import pytest

from telegram.errors import (
    AuthenticationError,
    ConfigurationError,
    DestinationError,
    DuplicateError,
    EncodingError,
    PermanentTelegramError,
    RateLimitError,
    TransientTelegramError,
)
from telegram.friendly import friendly_message, suggest_retry

FAKE_TOKEN = "123456:" + "A" * 35


def test_authentication_error_guidance() -> None:
    msg = friendly_message(AuthenticationError("getMe: invalid bot token"))
    assert "secrets/tokens.json" in msg
    assert "never paste the token into the workflow" in msg


def test_destination_error_guidance() -> None:
    msg = friendly_message(DestinationError("sendPhoto: chat not found"))
    assert "chat not found" in msg
    assert "confirm the bot is a member/admin" in msg


def test_rate_limit_with_retry_after_wording() -> None:
    msg = friendly_message(RateLimitError("slow down", retry_after=42))
    assert "42s" in msg
    assert "queue handles this in background mode" in msg


def test_rate_limit_without_retry_after() -> None:
    msg = friendly_message(RateLimitError("slow down"))
    assert "rate limit" in msg.lower()
    assert "queue handles this in background mode" in msg


def test_transient_guidance() -> None:
    msg = friendly_message(TransientTelegramError("connection failed"))
    assert "safe to retry without regenerating the image" in msg


def test_duplicate_passthrough() -> None:
    msg = friendly_message(DuplicateError("already published to dest-1"))
    assert "already published to dest-1" in msg


def test_encoding_error_guidance() -> None:
    msg = friendly_message(EncodingError("empty bytes"))
    assert msg.startswith("Image problem:")
    assert "empty bytes" in msg


def test_value_error_guidance() -> None:
    msg = friendly_message(ValueError("bad caption"))
    assert msg.startswith("Image problem:")
    assert "bad caption" in msg


def test_unknown_error_format() -> None:
    msg = friendly_message(RuntimeError("weird boom"))
    assert msg.startswith("Unexpected error (RuntimeError)")
    assert "weird boom" in msg


def test_unknown_permanent_error_mentions_type() -> None:
    msg = friendly_message(PermanentTelegramError("malformed data"))
    assert "PermanentTelegramError" in msg
    assert "malformed data" in msg


def test_redact_backstop_with_token_shaped_string() -> None:
    msg = friendly_message(ValueError(f"leaked {FAKE_TOKEN} value"))
    assert FAKE_TOKEN not in msg
    assert "***" in msg


def test_redact_backstop_unknown_action() -> None:
    msg = friendly_message(
        RuntimeError(f"boom {FAKE_TOKEN}"), action="connection test"
    )
    assert FAKE_TOKEN not in msg
    assert "***" in msg
    assert "connection test" in msg


def test_configuration_error_never_leaks_token_shape() -> None:
    msg = friendly_message(ConfigurationError(f"bad {FAKE_TOKEN} config"))
    assert FAKE_TOKEN not in msg


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TransientTelegramError("x"), True),
        (RateLimitError("x", retry_after=5), True),
        (RateLimitError("x"), True),
        (AuthenticationError("x"), False),
        (DestinationError("x"), False),
        (DuplicateError("x"), False),
        (EncodingError("x"), False),
        (ValueError("x"), False),
        (RuntimeError("x"), False),
        (ConfigurationError("x"), False),
    ],
)
def test_suggest_retry_matrix(exc, expected) -> None:
    assert suggest_retry(exc) is expected
