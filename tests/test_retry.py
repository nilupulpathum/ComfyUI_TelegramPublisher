"""T020: RetryPolicy unit tests. No network, no tokens, no logging."""

import pytest

from services.retry import RetryPolicy
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


def noop_policy(**kwargs):
    """RetryPolicy that never sleeps, returning (policy, sleeps)."""
    sleeps: list = []
    return RetryPolicy(sleep=sleeps.append, **kwargs), sleeps


# -- validation --------------------------------------------------------


def test_rejects_bad_max_attempts():
    for bad in (0, -1, -100):
        with pytest.raises(ValueError):
            RetryPolicy(sleep=lambda s: None, max_attempts=bad)


def test_rejects_negative_delays():
    for field in ("base_delay", "max_delay", "jitter"):
        with pytest.raises(ValueError):
            RetryPolicy(sleep=lambda s: None, **{field: -0.1})


def test_accepts_zero_delays():
    policy, _ = noop_policy(base_delay=0.0, max_delay=0.0, jitter=0.0)
    assert policy.delay_for(1, TransientTelegramError("x")) == 0.0


# -- backoff math -------------------------------------------------------


def test_transient_backoff_exact_without_jitter():
    policy, _ = noop_policy(base_delay=2.0, max_delay=100.0, jitter=0.0)
    assert policy.delay_for(1, TransientTelegramError("x")) == 2.0
    assert policy.delay_for(2, TransientTelegramError("x")) == 4.0
    assert policy.delay_for(3, TransientTelegramError("x")) == 8.0


def test_transient_backoff_capped_at_max_delay():
    policy, _ = noop_policy(base_delay=10.0, max_delay=25.0, jitter=0.0)
    assert policy.delay_for(1, TransientTelegramError("x")) == 10.0
    assert policy.delay_for(3, TransientTelegramError("x")) == 25.0
    assert policy.delay_for(10, TransientTelegramError("x")) == 25.0


def test_transient_jitter_bounds():
    policy, _ = noop_policy(base_delay=1.0, max_delay=100.0, jitter=0.5)
    for _ in range(50):
        delay = policy.delay_for(1, TransientTelegramError("x"))
        assert 1.0 <= delay <= 1.5


def test_transient_cap_plus_jitter_bound():
    policy, _ = noop_policy(base_delay=100.0, max_delay=7.0, jitter=0.5)
    for _ in range(50):
        delay = policy.delay_for(5, TransientTelegramError("x"))
        assert 7.0 <= delay <= 7.5


# -- should-retry matrix -------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (TransientTelegramError("timeout"), True),
        (RateLimitError("limited", retry_after=5), True),
        (RateLimitError("limited", retry_after=None), True),
        (AuthenticationError("bad token"), False),
        (DestinationError("bad chat"), False),
        (EncodingError("empty image"), False),
        (ConfigurationError("no token"), False),
        (PermanentTelegramError("malformed"), False),
        (DuplicateError("dup"), False),
        (ValueError("plain"), False),
        (KeyError("plain"), False),
    ],
)
def test_should_retry_matrix(exc, retryable):
    policy, _ = noop_policy()
    delay = policy.delay_for(1, exc)
    assert (delay is not None) is retryable


def test_rate_limit_honors_retry_after():
    policy, _ = noop_policy()
    assert policy.delay_for(1, RateLimitError("x", retry_after=5)) == 5.0


def test_rate_limit_without_retry_after_uses_backoff():
    policy, _ = noop_policy(base_delay=2.0, max_delay=100.0, jitter=0.0)
    assert policy.delay_for(1, RateLimitError("x", retry_after=None)) == 2.0
    assert policy.delay_for(2, RateLimitError("x")) == 4.0


def test_rate_limit_negative_retry_after_falls_back_to_backoff():
    policy, _ = noop_policy(base_delay=2.0, max_delay=100.0, jitter=0.0)
    assert policy.delay_for(1, RateLimitError("x", retry_after=-3)) == 2.0


def test_rate_limit_retry_after_capped_at_max_delay():
    policy, _ = noop_policy(max_delay=7.0)
    assert policy.delay_for(1, RateLimitError("x", retry_after=100)) == 7.0


# -- run() ---------------------------------------------------------------


def test_run_success_first_try_no_sleep_no_hook():
    policy, sleeps = noop_policy()
    calls: list = []
    hooked: list = []

    def fn():
        calls.append(1)
        return "ok"

    assert policy.run(fn, on_retry=lambda *a: hooked.append(a)) == "ok"
    assert len(calls) == 1
    assert sleeps == []
    assert hooked == []


def test_run_hook_called_with_attempt_exc_delay():
    policy, sleeps = noop_policy(base_delay=1.0, jitter=0.0)
    hooked: list = []
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < 3:
            raise TransientTelegramError(f"boom{state['n']}")
        return "ok"

    result = policy.run(
        fn, on_retry=lambda a, e, d: hooked.append((a, e, d))
    )
    assert result == "ok"
    assert state["n"] == 3
    assert [h[0] for h in hooked] == [1, 2]
    assert [str(h[1]) for h in hooked] == ["boom1", "boom2"]
    assert hooked[0][2] == sleeps[0] == 1.0
    assert hooked[1][2] == sleeps[1] == 2.0


def test_run_exhaustion_reraises_last_exception_unchanged():
    policy, sleeps = noop_policy()
    errs = [TransientTelegramError(f"boom{i}") for i in range(3)]
    calls: list = []

    def fn():
        exc = errs[len(calls)]
        calls.append(1)
        raise exc

    with pytest.raises(TransientTelegramError) as info:
        policy.run(fn)
    assert info.value is errs[2]
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_run_non_retryable_raises_immediately():
    policy, sleeps = noop_policy()
    err = AuthenticationError("bad token")
    calls: list = []

    def fn():
        calls.append(1)
        raise err

    with pytest.raises(AuthenticationError) as info:
        policy.run(fn)
    assert info.value is err
    assert len(calls) == 1
    assert sleeps == []


def test_run_plain_value_error_not_retried():
    policy, sleeps = noop_policy()
    calls: list = []

    def fn():
        calls.append(1)
        raise ValueError("broken")

    with pytest.raises(ValueError):
        policy.run(fn)
    assert len(calls) == 1
    assert sleeps == []


def test_run_never_exceeds_max_attempts():
    policy, sleeps = noop_policy(max_attempts=2)
    calls: list = []

    def fn():
        calls.append(1)
        raise TransientTelegramError("always")

    with pytest.raises(TransientTelegramError):
        policy.run(fn)
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_run_max_attempts_one_means_single_try():
    policy, sleeps = noop_policy(max_attempts=1)
    calls: list = []

    def fn():
        calls.append(1)
        raise TransientTelegramError("once")

    with pytest.raises(TransientTelegramError):
        policy.run(fn)
    assert len(calls) == 1
    assert sleeps == []
