"""Typed error hierarchy for the Telegram integration layer.

Security: Telegram bot tokens must never appear in exception messages,
reprs, or log records. The base class redacts token-looking substrings
from every string arg as a defensive backstop; callers must still never
interpolate the token into messages in the first place.
"""

from __future__ import annotations

import re

# Real bot tokens look like ``123456:ABC-DEF...`` where the secret part is
# 35 chars of [A-Za-z0-9_-]. Match that shape so redaction is precise and
# does not mangle ordinary text such as chat ids or timestamps.
_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{35}")


def redact(text: str) -> str:
    """Mask Telegram-bot-token-looking substrings with ``***``."""
    if not isinstance(text, str):
        return text
    return _TOKEN_RE.sub("***", text)


class TelegramPublisherError(Exception):
    """Base error for all Telegram publisher failures."""

    def __init__(self, message: str = "") -> None:
        safe = redact(message) if isinstance(message, str) else message
        super().__init__(safe)


class ConfigurationError(TelegramPublisherError):
    """Local misconfiguration (missing token, bad settings, ...)."""


class AuthenticationError(TelegramPublisherError):
    """The bot token was rejected (HTTP 401 / invalid token)."""


class DestinationError(TelegramPublisherError):
    """Bad chat id, unknown chat, or bot not allowed to post there."""


class EncodingError(TelegramPublisherError):
    """Image payload could not be built or was empty/invalid."""


class DuplicateError(TelegramPublisherError):
    """Image was already published (duplicate detection)."""


class RateLimitError(TelegramPublisherError):
    """Telegram rate limit hit (HTTP 429)."""

    def __init__(self, message: str = "", retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientTelegramError(TelegramPublisherError):
    """Retryable failure: network error, timeout, or Telegram 5xx."""


class PermanentTelegramError(TelegramPublisherError):
    """Non-retryable failure: malformed responses or unknown API errors."""
