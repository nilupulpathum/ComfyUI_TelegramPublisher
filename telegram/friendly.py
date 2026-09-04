"""User-friendly error mapping for the Epic 5 UX (T055).

Maps typed :mod:`telegram.errors` failures (and plain ``ValueError`` from
image validation) to actionable guidance for ComfyUI users. Defense in
depth: the final string always passes through :func:`telegram.errors.redact`
so a token-shaped substring can never leak even if wrapped detail
contained one.
"""

from __future__ import annotations

from telegram.errors import (
    AuthenticationError,
    DestinationError,
    DuplicateError,
    EncodingError,
    RateLimitError,
    TransientTelegramError,
    redact,
)


def _detail(exc: BaseException) -> str:
    try:
        text = str(exc).strip()
    except Exception:
        return ""
    return text


def friendly_message(exc: BaseException, *, action: str = "publish") -> str:
    """Return actionable user guidance for ``exc`` (never raises, redacted)."""
    if isinstance(exc, AuthenticationError):
        message = (
            "Telegram reports an invalid bot token. Check secrets/tokens.json "
            "for this account (never paste the token into the workflow), "
            "then test again."
        )
    elif isinstance(exc, DestinationError):
        detail = _detail(exc)
        guidance = (
            "Check the destination chat_id and confirm the bot is a "
            "member/admin of the chat/channel."
        )
        message = f"{detail} {guidance}" if detail else guidance
    elif isinstance(exc, RateLimitError):
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            message = (
                "Telegram rate limit hit; retry automatically after "
                f"{retry_after}s (the queue handles this in background mode)."
            )
        else:
            message = (
                "Telegram rate limit hit; retry automatically shortly "
                "(the queue handles this in background mode)."
            )
    elif isinstance(exc, TransientTelegramError):
        detail = _detail(exc)
        base = (
            "Temporary Telegram/network problem; safe to retry without "
            "regenerating the image."
        )
        message = f"{base} ({detail})" if detail else base
    elif isinstance(exc, DuplicateError):
        message = _detail(exc) or "Duplicate image: already published."
    elif isinstance(exc, (EncodingError, ValueError)):
        detail = _detail(exc)
        message = f"Image problem: {detail}" if detail else "Image problem."
    else:
        detail = _detail(exc)
        name = type(exc).__name__
        if detail:
            message = f"Unexpected error ({name}) during {action}: {detail}"
        else:
            message = f"Unexpected error ({name}) during {action}."
    return redact(message)


def suggest_retry(exc: BaseException) -> bool:
    """True when retrying is safe/sensible (transient or rate-limit)."""
    return isinstance(exc, (TransientTelegramError, RateLimitError))
