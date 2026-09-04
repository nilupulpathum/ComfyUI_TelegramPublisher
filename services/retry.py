"""Typed retry policy with exponential backoff (services layer).

Only transient failures are retried (spec section 6): network timeouts,
connection resets, HTTP 429, and Telegram 5xx -- surfaced as
:class:`telegram.errors.TransientTelegramError` and
:class:`telegram.errors.RateLimitError`. Everything else
(``AuthenticationError``, ``DestinationError``, ``EncodingError``,
``ConfigurationError``, ``PermanentTelegramError``, ``DuplicateError``,
plain ``ValueError``/``KeyError``, ...) is never retried.

FR-008 residual risk: ``sendPhoto``/``sendMediaGroup``/``sendMessage``
are not idempotent server-side, so a retry issued after a client-side
timeout (request reached Telegram, response was lost) can theoretically
publish a duplicate message. Bounded ``max_attempts`` plus honoring the
server's ``retry_after`` are the mitigation; callers needing stronger
guarantees should combine this with duplicate detection. Retries never
wrap or swallow: the last exception is re-raised unchanged.

Pure stdlib (``time``, ``random``, ``dataclasses``). No logging here;
pass ``on_retry`` to ``run`` so callers (e.g. T023) can log.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from telegram.errors import RateLimitError, TransientTelegramError

T = TypeVar("T")

#: Hook called before each sleep: (failed attempt number, exception, delay).
OnRetry = Callable[[int, BaseException, float], None]


@dataclass
class RetryPolicy:
    """Bounded exponential-backoff retry policy.

    ``sleep`` is injectable so tests can record delays without waiting;
    production uses :func:`time.sleep`.
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.5
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be an int >= 1")
        for name in ("base_delay", "max_delay", "jitter"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not value >= 0
            ):
                raise ValueError(f"{name} must be a non-negative number")

    def _backoff(self, attempt: int) -> float:
        capped = min(float(self.max_delay), float(self.base_delay) * (2.0 ** (attempt - 1)))
        return capped + random.uniform(0, float(self.jitter))

    def delay_for(self, attempt: int, exc: BaseException) -> float | None:
        """Delay in seconds before retrying, or None = do not retry."""
        if isinstance(exc, RateLimitError):
            retry_after = exc.retry_after
            if isinstance(retry_after, bool):
                retry_after = None
            if retry_after is not None and retry_after >= 0:
                return min(float(retry_after), float(self.max_delay))
            return self._backoff(attempt)
        if isinstance(exc, TransientTelegramError):
            return self._backoff(attempt)
        return None

    def run(
        self, fn: Callable[[], T], *, on_retry: OnRetry | None = None
    ) -> T:
        """Call ``fn()``, retrying retryable failures up to ``max_attempts``.

        Attempt counting starts at 1; total calls never exceed
        ``max_attempts``. On a retryable failure with attempts remaining,
        ``on_retry(attempt, exc, delay)`` fires, then ``sleep(delay)``,
        then retry. Otherwise the last exception is re-raised unchanged
        (never wrapped, never swallowed).
        """
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except Exception as exc:
                if attempt >= self.max_attempts:
                    raise
                delay = self.delay_for(attempt, exc)
                if delay is None:
                    raise
                if on_retry is not None:
                    on_retry(attempt, exc, delay)
                self.sleep(delay)
        raise AssertionError("unreachable: max_attempts >= 1 guarantees return/raise")
