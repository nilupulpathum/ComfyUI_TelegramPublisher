"""Structured logging with secret redaction (T023).

Logger records propagate to ComfyUI's console handling: this module
attaches no handlers, only a :class:`RedactingFilter` backstop so bot
tokens never leak through logs. Pure stdlib + :mod:`telegram.errors`.
"""

from __future__ import annotations

import logging
import re

from .errors import redact

# Matches the "/bot<token>" URL path segment, e.g.
# "https://api.telegram.org/botTEST_TOKEN_xxx/sendMessage" or
# "https://api.telegram.org/bot123456:ABC.../sendMessage".
# Replaced with "/bot***" so fake test tokens (which do not match the
# strict token shape in errors.redact) are still scrubbed.
_BOT_URL_RE = re.compile(r"/bot[^\s/]+")


def _scrub(text: str) -> str:
    """Apply token-shape redaction then bot-URL redaction."""
    return _BOT_URL_RE.sub("/bot***", redact(text))


class RedactingFilter(logging.Filter):
    """Scrub secrets from a log record's message and args.

    Redacts token-shaped substrings (via :func:`telegram.errors.redact`)
    plus ``/bot<token>`` URL shapes from both the formatted message and
    the record args. Never raises: any internal failure leaves the
    record in place and returns ``True`` (keep the record).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                try:
                    record.msg = _scrub(record.msg)
                except Exception:
                    pass
            args = record.args
            try:
                if isinstance(args, dict):
                    record.args = {
                        k: (_scrub(v) if isinstance(v, str) else v)
                        for k, v in args.items()
                    }
                elif isinstance(args, (tuple, list)):
                    scrubbed = tuple(
                        _scrub(a) if isinstance(a, str) else a for a in args
                    )
                    record.args = (
                        scrubbed if isinstance(args, tuple) else list(scrubbed)
                    )
                elif isinstance(args, str):
                    record.args = _scrub(args)
            except Exception:
                pass
            # Backstop: an arg may be a non-str object (e.g. an exception)
            # whose formatted rendering still carries a secret.
            try:
                formatted = record.getMessage()
            except Exception:
                formatted = None
            if isinstance(formatted, str):
                try:
                    cleaned = _scrub(formatted)
                except Exception:
                    cleaned = None
                if isinstance(cleaned, str) and cleaned != formatted:
                    record.msg = cleaned
                    record.args = None
        except Exception:
            pass
        return True


def get_logger(name: str) -> logging.Logger:
    """Return the named stdlib logger with exactly one RedactingFilter.

    Idempotent: repeated calls attach no additional filter and add no
    handlers (records propagate to ComfyUI's console handling).
    """
    logger = logging.getLogger(name)
    try:
        for existing in list(getattr(logger, "filters", [])):
            if isinstance(existing, RedactingFilter):
                return logger
        logger.addFilter(RedactingFilter())
    except Exception:
        pass
    return logger
