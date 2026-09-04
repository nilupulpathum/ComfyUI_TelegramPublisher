"""Bounded background publish queue with graceful shutdown (T041 + T044).

Architecture constraints (docs/ARCHITECTURE.md section 6):

- persist the job before enqueueing;
- use a bounded queue;
- worker lifecycle tied to ComfyUI;
- never lose the local job record silently;
- expose queue state.

Design:

- :class:`PublishPayload` carries the in-memory bytes plus routing info.
  The ``sender`` callable owns all network/token handling: it performs the
  actual upload and returns the Telegram message id(s) string, raising
  typed :mod:`telegram.errors` on failure. This module never touches the
  network or tokens (token-free by construction).
- Payloads are held in an in-memory dict keyed by ``job_id`` with a hard
  bound (``maxsize``). Enqueue beyond the bound raises ``ValueError``
  LOUDLY and creates no row -- never silently drop.
- ``enqueue`` inserts the ``publish_jobs`` row (status ``"queued"``)
  FIRST, then buffers the payload, under one lock. A crash between the
  two is impossible from the caller's view: the row insert happens before
  the buffer insert inside the same critical section, so a "kill-buffer
  assert row exists" test passes.
- Worker thread: daemon ``True`` so a wedged ``sender`` call can never
  hang interpreter exit; the graceful path is :meth:`PublishQueue.stop`
  (event + join, in-flight sender runs to completion because ``join``
  waits for it). Process-wide queues from :func:`get_shared_queue` are
  additionally covered by an ``atexit`` hook
  (:func:`_shutdown_shared_queues`) as a backstop, because ComfyUI has
  no extension-unload hook. On hard process kill the persisted
  ``queued``/``sending`` rows survive and are picked up on next start.
- Missing payload (e.g. after a process restart the bytes are gone while
  the row survived): the record is preserved -- the row goes to
  ``"failed"`` with ``error_code="PayloadLost"`` plus an error log.
  Nothing is deleted silently.
- Transient failures (``TransientTelegramError``/``RateLimitError`` only)
  requeue as ``"queued"`` with ``next_retry_at`` computed from
  ``RetryPolicy.delay_for`` (or a +30s default when no policy is set) and
  ``attempts`` persisted. When ``attempts >= max_attempts`` (default 5,
  bounded) the job goes to ``"failed"`` with ``error_code="RetryExhausted"``.
  Everything else (any other telegram error, ``ValueError``, ...) is
  permanent and fails the row immediately with ``error_code=type(exc)``.
- T042 scheduling (implemented): server-directed ``retry_after`` is
  honored precisely via ``RetryPolicy.delay_for`` (``min(retry_after,
  max_delay)``), verified by ``tests/test_background.py``; and a worker
  that takes a job whose ``next_retry_at`` is still in the future
  (clock skew/race) puts it back untouched and sleeps until due
  (capped), instead of attempting early or busy-spinning. See
  :meth:`PublishQueue._process_one` and :func:`_seconds_until_due`.
- T043 status (implemented): :class:`QueueStatus` plus
  :meth:`PublishQueue.status` built from ``count_by_status()`` plus
  worker/buffer state. Node wiring passes
  ``GenerationMetadata.as_template_vars()`` dicts in as ``metadata``.

Thread-safety: one :class:`threading.Lock` guards the buffer dict plus
the started/shutdown flags. SQLite writes use short connections; the
worker is the only writer besides ``enqueue``'s insert (single-writer,
fine). The ``sender`` call always runs OUTSIDE the lock.
"""

from __future__ import annotations

import atexit
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from services.retry import RetryPolicy
from storage.database import Database, utcnow_iso
from storage.repositories import (
    Generation,
    GenerationRepository,
    PublishJob,
    PublishJobRepository,
)
from telegram.errors import RateLimitError, TransientTelegramError
from telegram.logging import get_logger

#: Delay used for transient requeue when no RetryPolicy is configured.
DEFAULT_RETRY_DELAY_S = 30.0

_TRANSIENT_ERRORS = (TransientTelegramError, RateLimitError)


def _new_job_id() -> str:
    return uuid.uuid4().hex


def _retry_at_iso(delay_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=float(delay_s))).isoformat()


def _seconds_until_due(next_retry_at: str | None) -> float:
    """Seconds until a queued job is due; ``<= 0`` means due now.

    ``None``/empty means immediately due. Unparseable timestamps are
    treated as due (fail-open) so a corrupt value can never park a job
    forever. Naive datetimes are assumed UTC (all writers store UTC ISO).
    """
    if not next_retry_at:
        return 0.0
    try:
        due = datetime.fromisoformat(str(next_retry_at))
    except Exception:
        return 0.0
    try:
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return (due - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return 0.0


@dataclass
class PublishPayload:
    """In-memory publish request buffered by :class:`PublishQueue`.

    ``metadata`` is a plain dict (typically
    ``GenerationMetadata.as_template_vars()`` plus ``filename``); the queue
    reads ``metadata.get("image_hash")`` for the history row when present.
    ``job_id`` is None until :meth:`PublishQueue.enqueue` fills it.
    """

    kind: str
    account_id: str
    destination_id: str
    chat_id: str
    files: tuple[tuple[bytes, str], ...]
    caption: str | None = None
    protect_content: bool = False
    disable_notification: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("photo", "album"):
            raise ValueError("kind must be 'photo' or 'album'")
        for name in ("account_id", "destination_id", "chat_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.files, tuple) or len(self.files) == 0:
            raise ValueError("files must be a non-empty tuple of (bytes, filename)")
        for item in self.files:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], (bytes, bytearray))
                or not isinstance(item[1], str)
                or not item[1].strip()
            ):
                raise ValueError("files entries must be (bytes, filename) pairs")
        if self.caption is not None and not isinstance(self.caption, str):
            raise ValueError("caption must be a string or None")
        if self.metadata is None:
            self.metadata = {}
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")
        if self.job_id is not None and (
            not isinstance(self.job_id, str) or not self.job_id.strip()
        ):
            raise ValueError("job_id must be a non-empty string or None")


@dataclass
class QueueStatus:
    """Point-in-time queue snapshot (T043, API only).

    ``queued``/``sending``/``success``/``failed`` are SQLite row counts
    (missing status keys read as 0); ``worker_running`` mirrors
    :attr:`PublishQueue.worker_alive`; ``buffered`` is the in-memory
    payload count; ``maxsize`` is the buffer bound. The node-level
    status indicator UI is Epic 5 T054 and is not part of this API.
    """

    queued: int = 0
    sending: int = 0
    success: int = 0
    failed: int = 0
    worker_running: bool = False
    buffered: int = 0
    maxsize: int = 0


class PublishQueue:
    """Bounded FIFO publish queue with a single background worker."""

    def __init__(
        self,
        db: Database,
        sender: Callable[[PublishPayload], str],
        *,
        maxsize: int = 100,
        poll_interval: float = 1.0,
        retry_policy: RetryPolicy | None = None,
        max_attempts: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        if not callable(sender):
            raise ValueError("sender must be callable")
        if (
            not isinstance(maxsize, int)
            or isinstance(maxsize, bool)
            or maxsize <= 0
        ):
            raise ValueError("maxsize must be a positive int")
        if (
            not isinstance(poll_interval, (int, float))
            or isinstance(poll_interval, bool)
            or not poll_interval >= 0
        ):
            raise ValueError("poll_interval must be a non-negative number")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be an int >= 1")
        self._db = db
        self._sender = sender
        self._maxsize = int(maxsize)
        self._poll_interval = float(poll_interval)
        self._retry_policy = retry_policy
        self._max_attempts = int(max_attempts)
        self._log = logger if logger is not None else get_logger("services.queue")
        self._jobs = PublishJobRepository(db)
        self._lock = threading.Lock()
        self._buffer: dict[str, PublishPayload] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._shutdown = False

    # -- state ---------------------------------------------------------
    @property
    def worker_alive(self) -> bool:
        """True while the worker thread is running."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def depth(self) -> int:
        """Number of payloads currently buffered in memory."""
        with self._lock:
            return len(self._buffer)

    def pending_jobs(self) -> int:
        """Number of ``queued`` rows in SQLite (due or scheduled)."""
        try:
            return int(self._jobs.count_by_status().get("queued", 0))
        except Exception:
            return 0

    def status(self) -> QueueStatus:
        """Return a point-in-time :class:`QueueStatus` snapshot.

        Status counts come from ``count_by_status()`` (missing keys
        default to 0; a repository failure yields all-zero counts, never
        a raise). ``buffered``/``maxsize`` describe the in-memory bound;
        ``worker_running`` mirrors :attr:`worker_alive`.
        """
        try:
            counts = self._jobs.count_by_status()
        except Exception:
            counts = {}

        def _count(key: str) -> int:
            try:
                return int(counts.get(key, 0))
            except Exception:
                return 0

        with self._lock:
            buffered = len(self._buffer)
            maxsize = self._maxsize
        return QueueStatus(
            queued=_count("queued"),
            sending=_count("sending"),
            success=_count("success"),
            failed=_count("failed"),
            worker_running=self.worker_alive,
            buffered=buffered,
            maxsize=maxsize,
        )

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        """Start the worker thread (idempotent).

        Raises:
            ValueError: If the queue was shut down via :meth:`stop`
                (one queue per process; no restart after stop).
        """
        with self._lock:
            if self._shutdown:
                raise ValueError("queue is shut down")
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="publish-queue-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Signal shutdown and wait for the worker.

        The in-flight ``sender`` call (if any) runs to completion; ``join``
        waits for it up to ``timeout`` seconds.

        Returns True when the worker thread is no longer alive, False on
        timeout (plus an error log). Never raises for an unstarted queue.
        """
        with self._lock:
            self._shutdown = True
            thread = self._thread
        self._stop_event.set()
        if thread is None:
            return True
        thread.join(timeout=timeout)
        alive = thread.is_alive()
        if alive:
            try:
                self._log.error(
                    "publish queue worker did not stop within %.2fs", timeout
                )
            except Exception:
                pass
            return False
        return True

    def __enter__(self) -> PublishQueue:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- enqueue -------------------------------------------------------
    def enqueue(self, payload: PublishPayload) -> str:
        """Persist a job row, then buffer the payload. Return the job id.

        Works before :meth:`start` (jobs persist; the worker picks them up
        on start). Raises ``ValueError`` loudly when shut down or when the
        bounded buffer is full -- creating no row in the full case.
        """
        if not isinstance(payload, PublishPayload):
            raise ValueError("payload must be a PublishPayload instance")
        with self._lock:
            if self._shutdown:
                raise ValueError("queue is shut down")
            if len(self._buffer) >= self._maxsize:
                raise ValueError("publish queue is full")
            if payload.job_id:
                job_id = payload.job_id
            else:
                job_id = _new_job_id()
                payload.job_id = job_id
            image_hash: str | None = None
            filename: str | None = None
            try:
                meta_hash = payload.metadata.get("image_hash")
            except Exception:
                meta_hash = None
            if isinstance(meta_hash, str) and meta_hash:
                image_hash = meta_hash
            try:
                if payload.files:
                    filename = payload.files[0][1]
            except Exception:
                filename = None
            if not filename:
                try:
                    meta_name = payload.metadata.get("filename")
                except Exception:
                    meta_name = None
                if isinstance(meta_name, str) and meta_name:
                    filename = meta_name
            job = PublishJob(
                id=job_id,
                destination_id=payload.destination_id,
                status="queued",
                image_hash=image_hash,
                filename=filename,
                caption=payload.caption,
                attempts=0,
                next_retry_at=None,
            )
            # Persist BEFORE buffering (architecture constraint). Both happen
            # inside the lock so a full-buffer check and the insert are atomic.
            self._jobs.create(job)
            self._buffer[job_id] = payload
            return job_id

    # -- worker --------------------------------------------------------
    def _next_retry_at(self, attempts: int, exc: BaseException) -> str:
        """Compute ``next_retry_at`` for a transient failure (T042 hook)."""
        delay: float | None = None
        if self._retry_policy is not None:
            try:
                delay = self._retry_policy.delay_for(attempts, exc)
            except Exception:
                delay = None
        if delay is None:
            delay = DEFAULT_RETRY_DELAY_S
        try:
            delay_f = float(delay)
        except Exception:
            delay_f = DEFAULT_RETRY_DELAY_S
        if not delay_f >= 0:
            delay_f = DEFAULT_RETRY_DELAY_S
        return _retry_at_iso(delay_f)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                due = self._jobs.find_due(limit=1)
            except Exception as exc:
                try:
                    self._log.warning("publish queue find_due failed: %s", exc)
                except Exception:
                    pass
                self._stop_event.wait(self._poll_interval)
                continue
            if not due:
                self._stop_event.wait(self._poll_interval)
                continue
            try:
                wait_s = self._process_one(due[0].id)  # type: ignore[arg-type]
            except Exception as exc:
                try:
                    self._log.warning("publish queue worker error: %s", exc)
                except Exception:
                    pass
                continue
            if isinstance(wait_s, float) and wait_s > 0:
                # A not-due job was put back (clock skew/race): sleep
                # until due, capped, instead of busy-spinning. A
                # non-positive poll_interval (degenerate test config)
                # falls back to the full wait so the loop still idles.
                cap = self._poll_interval * 5.0
                sleep_due = min(wait_s, cap) if cap > 0 else wait_s
                self._stop_event.wait(sleep_due)

    def _process_one(self, job_id: str) -> float | None:
        # Claim: queued -> sending. Row vanished -> drop payload + warning.
        try:
            job = self._jobs.get(job_id)
        except Exception as exc:
            try:
                self._log.warning(
                    "publish queue job %s read failed: %s", job_id, exc
                )
            except Exception:
                pass
            return
        if job is None:
            with self._lock:
                self._buffer.pop(job_id, None)
            try:
                self._log.warning(
                    "publish queue job %s vanished; dropped buffered payload",
                    job_id,
                )
            except Exception:
                pass
            return
        if job.status != "queued":
            return None
        due_in = _seconds_until_due(job.next_retry_at)
        if due_in > 0:
            # T042 due-wakeup: find_due said due but the row is not due
            # yet (clock skew/race). Leave the row AND the buffered
            # payload untouched -- no claim, no attempts increment, no
            # upload -- and report the delay so _run sleeps until due
            # (capped) instead of busy-spinning.
            return due_in
        job.status = "sending"
        job.next_retry_at = None
        try:
            self._jobs.update(job)
        except KeyError:
            with self._lock:
                self._buffer.pop(job_id, None)
            try:
                self._log.warning(
                    "publish queue job %s vanished on claim; dropped payload",
                    job_id,
                )
            except Exception:
                pass
            return
        except Exception as exc:
            try:
                self._log.warning(
                    "publish queue job %s claim failed: %s", job_id, exc
                )
            except Exception:
                pass
            return

        with self._lock:
            payload = self._buffer.pop(job_id, None)

        if payload is None:
            # Bytes are gone (e.g. process restart); preserve the record.
            try:
                lost = self._jobs.get(job_id)
            except Exception:
                lost = None
            if lost is not None:
                lost.status = "failed"
                lost.error_code = "PayloadLost"
                lost.error_message = (
                    "payload bytes unavailable (process restart or "
                    "enqueue not completed); record preserved"
                )
                lost.next_retry_at = None
                try:
                    self._jobs.update(lost)
                except Exception:
                    pass
            try:
                self._log.error(
                    "publish queue job %s payload missing; marked failed",
                    job_id,
                )
            except Exception:
                pass
            return

        # attempts += 1 persisted BEFORE the upload attempt.
        job.attempts += 1
        try:
            self._jobs.update(job)
        except Exception as exc:
            # Do not lose the payload: put it back for a later attempt.
            with self._lock:
                if len(self._buffer) < self._maxsize:
                    self._buffer[job_id] = payload
            try:
                self._log.warning(
                    "publish queue job %s attempts persist failed: %s",
                    job_id,
                    exc,
                )
            except Exception:
                pass
            return

        # Upload OUTSIDE any lock.
        try:
            message_id = self._sender(payload)
        except _TRANSIENT_ERRORS as exc:
            self._handle_transient(job, payload, exc)
            return
        except Exception as exc:
            self._fail(job, type(exc).__name__, str(exc))
            return
        self._succeed(job, payload, message_id)

    def _succeed(
        self, job: PublishJob, payload: PublishPayload, message_id: object
    ) -> None:
        try:
            fresh = self._jobs.get(job.id)  # type: ignore[arg-type]
        except Exception:
            fresh = job
        target = fresh if fresh is not None else job
        # Generation row FIRST so a visible "success" row always implies its
        # generation row is already queryable (best-effort, never fails
        # the already-successful publish).
        self._record_worker_generation(target.id, payload.metadata)
        target.status = "success"
        target.telegram_message_id = (
            None if message_id is None else str(message_id)
        )
        target.error_code = None
        target.error_message = None
        target.next_retry_at = None
        try:
            self._jobs.update(target)
        except Exception as exc:
            try:
                self._log.warning(
                    "publish queue job %s success persist failed: %s",
                    target.id,
                    exc,
                )
            except Exception:
                pass
            return

    def _record_worker_generation(self, job_id: object, metadata: object) -> None:
        """Persist one generation row from payload template vars (T035)."""
        try:
            vars_map = dict(metadata) if isinstance(metadata, dict) else {}
        except Exception:
            return

        def _text(key: str) -> str | None:
            value = vars_map.get(key)
            if value is None:
                return None
            text = str(value)
            return text if text else None

        def _number(key: str) -> int | None:
            value = vars_map.get(key)
            if value is None or value == "":
                return None
            try:
                return int(str(value))
            except (TypeError, ValueError):
                return None

        try:
            GenerationRepository(self._db).create(
                Generation(
                    job_id=str(job_id),
                    prompt=_text("prompt"),
                    negative_prompt=_text("negative_prompt"),
                    model=_text("model"),
                    seed=_text("seed"),
                    steps=_text("steps"),
                    cfg=_text("cfg"),
                    sampler=_text("sampler"),
                    scheduler=_text("scheduler"),
                    width=_number("width"),
                    height=_number("height"),
                )
            )
        except Exception as exc:
            try:
                self._log.warning(
                    "publish queue job %s generation persist failed: %s",
                    job_id,
                    exc,
                )
            except Exception:
                pass

    def _fail(self, job: PublishJob, code: str, message: str) -> None:
        try:
            fresh = self._jobs.get(job.id)  # type: ignore[arg-type]
        except Exception:
            fresh = job
        target = fresh if fresh is not None else job
        target.status = "failed"
        target.error_code = code
        target.error_message = message
        target.next_retry_at = None
        try:
            self._jobs.update(target)
        except Exception as exc:
            try:
                self._log.warning(
                    "publish queue job %s failure persist failed: %s",
                    target.id,
                    exc,
                )
            except Exception:
                pass
        # Payload already popped: cleared (terminal). Log at warning level;
        # error content is token-free by construction.
        try:
            self._log.warning(
                "publish queue job %s failed (%s)", target.id, code
            )
        except Exception:
            pass

    def _handle_transient(
        self, job: PublishJob, payload: PublishPayload, exc: BaseException
    ) -> None:
        job_id = job.id
        assert job_id is not None
        if job.attempts >= self._max_attempts:
            try:
                fresh = self._jobs.get(job_id)
            except Exception:
                fresh = job
            target = fresh if fresh is not None else job
            target.status = "failed"
            target.error_code = "RetryExhausted"
            try:
                detail = str(exc)
            except Exception:
                detail = ""
            target.error_message = (
                f"retry exhausted after {target.attempts} attempts: {detail}"
            )
            target.next_retry_at = None
            try:
                self._jobs.update(target)
            except Exception:
                pass
            try:
                self._log.warning(
                    "publish queue job %s retry exhausted after %d attempts",
                    job_id,
                    target.attempts,
                )
            except Exception:
                pass
            return
        next_retry_at = self._next_retry_at(job.attempts, exc)
        try:
            fresh = self._jobs.get(job_id)
        except Exception:
            fresh = job
        target = fresh if fresh is not None else job
        target.status = "queued"
        target.next_retry_at = next_retry_at
        try:
            err_name = type(exc).__name__
        except Exception:
            err_name = "TransientTelegramError"
        try:
            err_msg = str(exc)
        except Exception:
            err_msg = ""
        target.error_code = err_name
        target.error_message = err_msg
        try:
            self._jobs.update(target)
        except Exception:
            pass
        # Put the payload back for the scheduled retry. Buffer cannot be
        # over full here: this payload was just popped, so there is room.
        with self._lock:
            if len(self._buffer) >= self._maxsize:
                # Extremely defensive: another thread filled the slot while
                # the sender ran. Preserve the record loudly instead of
                # dropping silently.
                try:
                    stuck = self._jobs.get(job_id)
                except Exception:
                    stuck = None
                if stuck is not None:
                    stuck.status = "failed"
                    stuck.error_code = "PayloadLost"
                    stuck.error_message = (
                        "buffer full on transient requeue; record preserved"
                    )
                    stuck.next_retry_at = None
                    try:
                        self._jobs.update(stuck)
                    except Exception:
                        pass
                try:
                    self._log.error(
                        "publish queue job %s lost on requeue (buffer full)",
                        job_id,
                    )
                except Exception:
                    pass
                return
            self._buffer[job_id] = payload
        try:
            self._log.warning(
                "publish queue job %s transient %s; retry at %s",
                job_id,
                target.error_code,
                next_retry_at,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared process-wide queues (Epic 4 node wiring)
# ---------------------------------------------------------------------------

_SHARED_QUEUES: dict[str, PublishQueue] = {}
_SHARED_QUEUES_LOCK = threading.Lock()


def get_shared_queue(
    key: str, factory: Callable[[], PublishQueue]
) -> PublishQueue:
    """Return the process-wide started queue for ``key``.

    The first call per key invokes ``factory()`` exactly once, starts the
    queue, caches it, and returns it; later calls with the same key
    return the same started instance without calling the factory again.
    ``factory`` must take no arguments and return a :class:`PublishQueue`
    (anything else raises ``ValueError`` loudly and caches nothing).
    ``factory`` must not call back into :func:`get_shared_queue`
    (non-reentrant lock). A queue that fails to start is not cached, so
    a later call retries the factory.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string")
    if not callable(factory):
        raise ValueError("factory must be callable")
    with _SHARED_QUEUES_LOCK:
        existing = _SHARED_QUEUES.get(key)
        if existing is not None:
            return existing
        queue = factory()
        if not isinstance(queue, PublishQueue):
            raise ValueError("factory must return a PublishQueue instance")
        try:
            queue.start()  # idempotent: already-started is a no-op
        except Exception:
            raise
        _SHARED_QUEUES[key] = queue
        return queue


def _shutdown_shared_queues(timeout: float = 5.0) -> None:
    """Stop every shared queue (best-effort interpreter-shutdown backstop)."""
    with _SHARED_QUEUES_LOCK:
        queues = list(_SHARED_QUEUES.values())
        _SHARED_QUEUES.clear()
    for queue in queues:
        try:
            queue.stop(timeout=timeout)
        except Exception:
            # Swallowed ONLY here at interpreter shutdown: an atexit
            # handler must never raise (it would print to stderr during
            # teardown and can mask the real exit cause). Everywhere else
            # in this codebase exceptions propagate loudly instead.
            pass


atexit.register(_shutdown_shared_queues)
