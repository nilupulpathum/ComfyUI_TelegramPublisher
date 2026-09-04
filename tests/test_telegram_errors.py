"""T004: Telegram error hierarchy, retry_after, and token redaction.

Uses only fake token-shaped strings (``TEST_TOKEN_xxx`` style and
synthetic ``digits:35-char-secret`` values). No network, no real tokens.
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
    TelegramPublisherError,
    TransientTelegramError,
    redact,
)

# Synthetic token-shaped value: digits + ':' + 35-char secret. Fake only.
FAKE_TOKEN = "123456:" + "A" * 35


def test_hierarchy():
    for cls in (
        ConfigurationError,
        AuthenticationError,
        DestinationError,
        EncodingError,
        DuplicateError,
        RateLimitError,
        TransientTelegramError,
        PermanentTelegramError,
    ):
        assert issubclass(cls, TelegramPublisherError)


def test_rate_limit_carries_retry_after():
    err = RateLimitError("slow down", retry_after=42)
    assert err.retry_after == 42
    assert isinstance(err, TelegramPublisherError)


def test_rate_limit_retry_after_defaults_none():
    assert RateLimitError("slow down").retry_after is None


def test_redact_masks_token_shape():
    text = f"failed with {FAKE_TOKEN} for bot"
    out = redact(text)
    assert FAKE_TOKEN not in out
    assert "***" in out
    assert "for bot" in out


def test_redact_leaves_plain_text():
    assert redact("chat not found") == "chat not found"


@pytest.mark.parametrize(
    "cls,kwargs",
    [
        (ConfigurationError, {}),
        (AuthenticationError, {}),
        (DestinationError, {}),
        (EncodingError, {}),
        (DuplicateError, {}),
        (TransientTelegramError, {}),
        (PermanentTelegramError, {}),
    ],
)
def test_no_error_message_contains_token(cls, kwargs):
    err = cls(f"boom {FAKE_TOKEN} boom")
    assert FAKE_TOKEN not in str(err)
    assert FAKE_TOKEN not in repr(err)


def test_rate_limit_message_contains_no_token():
    err = RateLimitError(f"limited {FAKE_TOKEN}", retry_after=5)
    assert FAKE_TOKEN not in str(err)
    assert err.retry_after == 5
