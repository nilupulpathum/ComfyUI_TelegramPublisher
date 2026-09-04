"""Pending-review image payload store (T063) + TTL expiry (T071).

Filesystem companion to the ``pending_review`` publish-job rows: while a
generation awaits admin approval, its first-frame PNG bytes live ONLY
under the review directory as ``<jobid>.png``. Stdlib only at runtime
(the repository import below is ``TYPE_CHECKING``-only); no ComfyUI,
no network, no secrets.

Security properties:

- Job ids are validated against ``[A-Za-z0-9_-]{1,64}`` BEFORE any
  filesystem use, so a hostile ``/approve ../../x`` argument can never
  escape the review directory (path traversal guard).
- Payloads must be non-empty, PNG-magic-checked (``89 50 4E 47``), and
  ``<= 10 MB``; anything else raises ``ValueError`` and is never stored.
- Missing payloads raise ``ValueError`` with a clear ``"review payload
  missing"`` message (kept simple: no ``FileNotFoundError`` subclass).

Expiry (T071): abandoned ``pending_review`` rows/payloads accumulate
until approved/rejected, so :func:`prune_expired` reclaims them after
:data:`DEFAULT_REVIEW_TTL_S`. Only ``*.png`` files directly under the
review directory are considered; job ids come from file stems and are
re-validated with :func:`validate_job_id` (anything else is a stray
operator file: kept + warning, never deleted). Fail-safe is toward
PRESERVATION: unparseable ``created_at`` timestamps, unexpected job
statuses, and any per-file error keep the file and count it as
``"kept"`` with a warning -- nothing is deleted unless the row says
so. The garbage-collection host is the Telegram Command Poller node
(``publisher_nodes/poll_commands.py``), which calls :func:`prune_expired`
best-effort once per ``poll()`` run before polling.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only, runtime stays stdlib-only
    from storage.repositories import PublishJobRepository

_logger = get_logger(__name__)

#: Directory name holding review payloads next to the history database.
DEFAULT_REVIEW_DIRNAME = "review"

#: Max accepted review payload size (10 MB).
MAX_REVIEW_BYTES = 10 * 1024 * 1024

#: Default review TTL: abandoned ``pending_review`` payloads older than
#: this (7 days, in seconds) are marked ``failed``/``Expired`` and swept.
DEFAULT_REVIEW_TTL_S = 7 * 24 * 3600

#: Terminal job statuses: a leftover payload file for one of these rows is
#: swept WITHOUT touching the record (``"orphans"`` bucket).
_TERMINAL_STATUSES = frozenset({"success", "failed"})

#: Status recorded on a review job that aged out without approval. The row
#: is preserved (history is never deleted); only the PNG file is removed.
EXPIRED_ERROR_CODE = "Expired"
EXPIRED_ERROR_MESSAGE = "review expired without approval"

#: PNG file signature prefix (``89 50 4E 47 ...``).
_PNG_MAGIC = bytes((0x89, 0x50, 0x4E, 0x47))

_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def validate_job_id(job_id: str) -> str:
    """Return ``job_id`` when it is filesystem-safe, else raise ValueError.

    Raises:
        ValueError: If ``job_id`` is not a ``str`` matching
            ``[A-Za-z0-9_-]{1,64}`` (full match).
    """
    if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
        raise ValueError(
            f"invalid review job id {job_id!r}: must match [A-Za-z0-9_-]{{1,64}}"
        )
    return job_id


def review_dir_for(db_path: str | Path) -> Path:
    """Return the review directory for a history database path.

    The review directory is ``<db parent>/review`` (``DEFAULT_REVIEW_DIRNAME``).
    """
    return Path(db_path).parent / DEFAULT_REVIEW_DIRNAME


class ReviewStore:
    """Save/load/discard pending-review PNG payloads under one directory."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)

    @property
    def directory(self) -> Path:
        return self._dir

    def _path_for(self, job_id: str) -> Path:
        # Validated BEFORE any join: traversal-safe by construction since
        # a valid id cannot contain "/" or "..".
        validate_job_id(job_id)
        return self._dir / f"{job_id}.png"

    def save(self, job_id: str, png_bytes: bytes) -> Path:
        """Store ``png_bytes`` as ``<job_id>.png``; return the file path.

        Raises:
            ValueError: If ``job_id`` is not filesystem-safe, ``png_bytes``
                is not non-empty bytes, lacks the PNG magic prefix, or
                exceeds :data:`MAX_REVIEW_BYTES`.
        """
        path = self._path_for(job_id)
        if not isinstance(png_bytes, (bytes, bytearray)) or len(png_bytes) == 0:
            raise ValueError("review payload must be non-empty bytes")
        if len(png_bytes) > MAX_REVIEW_BYTES:
            raise ValueError(
                f"review payload exceeds {MAX_REVIEW_BYTES} bytes; refused"
            )
        if bytes(png_bytes[:4]) != _PNG_MAGIC:
            raise ValueError("review payload is not a PNG image; refused")
        if self._dir != Path():
            self._dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(png_bytes))
        return path

    def load(self, job_id: str) -> bytes:
        """Return the stored payload for ``job_id``.

        Raises:
            ValueError: If ``job_id`` is not filesystem-safe, or no
                review payload exists for it (``"review payload missing
                for job '<id>'"``).
        """
        path = self._path_for(job_id)
        try:
            return path.read_bytes()
        except OSError:
            raise ValueError(
                f"review payload missing for job '{job_id}'"
            ) from None

    def discard(self, job_id: str) -> None:
        """Delete the stored payload; idempotent (missing file is a no-op).

        Raises:
            ValueError: If ``job_id`` is not filesystem-safe.
        """
        path = self._path_for(job_id)
        try:
            path.unlink(missing_ok=True)
        except IsADirectoryError:
            # A directory at the payload path is never a valid payload;
            # leave it alone rather than failing the caller.
            pass
        except OSError:
            # Best-effort cleanup: a stale file must never break approve/reject.
            pass

    def exists(self, job_id: str) -> bool:
        """Return True when a payload file exists for ``job_id``.

        Raises:
            ValueError: If ``job_id`` is not filesystem-safe.
        """
        return self._path_for(job_id).is_file()


def _created_epoch(created_at: Any) -> float | None:
    """Return ``created_at`` (UTC ISO string) as epoch seconds, or None.

    None is returned for missing, empty, or unparseable values (callers
    treat those as "keep + warning": fail-safe toward preservation).
    Naive datetimes are assumed UTC; a trailing ``Z`` is accepted.
    """
    if not isinstance(created_at, str) or not created_at.strip():
        return None
    text = created_at.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _remove_payload(path: Path, job_id: str) -> bool:
    """Delete ``path``; return True on success (missing counts as success).

    Never raises: an ``OSError`` warns (token-free: job id only) and
    returns False so the caller keeps the file counted as ``"kept"``.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _logger.warning(
            "review expiry: cannot delete payload job_id=%s error=%s",
            job_id,
            exc,
        )
        return False
    return True


def prune_expired(
    review_dir: str | Path,
    jobs: PublishJobRepository,
    *,
    ttl_s: float = DEFAULT_REVIEW_TTL_S,
    now: float | None = None,
) -> dict[str, int]:
    """Expire abandoned review payloads under ``review_dir``.

    For each ``*.png`` file directly under ``review_dir`` the job id is
    the file stem, re-validated with :func:`validate_job_id`:

    - stem is not a valid job id -> stray operator file: kept + warning
      (never deleted);
    - no job row for the stem -> file deleted, ``"expired"`` += 1 (there
      is no record to mark; the payload alone is reclaimed);
    - row terminal (``success``/``failed``) with a leftover file -> file
      deleted, record preserved, ``"orphans"`` += 1;
    - row ``pending_review`` with ``created_at`` older than ``ttl_s``
      seconds (``now - created_at > ttl_s``) -> row marked ``failed``
      (``error_code="Expired"``, ``message="review expired without
      approval"``; the record is preserved) + file deleted,
      ``"expired"`` += 1;
    - row ``pending_review`` within TTL -> kept, ``"kept"`` += 1;
    - row ``pending_review`` with missing/unparseable ``created_at`` ->
      kept + warning (fail-safe toward PRESERVATION), ``"kept"`` += 1;
    - row with any other (non-terminal, non-``pending_review``) status ->
      kept + warning, ``"kept"`` += 1.

    ``now`` is epoch seconds (defaults to :func:`time.time`); pass an
    explicit value in tests for deterministic boundaries. A future
    ``created_at`` yields a negative age and is kept (clock-skew safe).

    Non-``*.png`` files and non-file matches (e.g. a directory named
    ``x.png``) are ignored entirely (not counted). A missing review
    directory yields all-zero counts.

    Never raises on per-file errors: lookup/update/delete failures warn
    (token-free: job id and error only) and count the file as ``"kept"``.
    A directory-level scan failure likewise warns and returns the counts
    accumulated so far.

    Args:
        review_dir: Directory holding ``<jobid>.png`` payloads.
        jobs: Repository used to resolve/mark job rows by file stem.
        ttl_s: Max age in seconds for a ``pending_review`` row.
        now: Epoch seconds used as "current time" (for tests).

    Returns:
        ``{"expired": int, "orphans": int, "kept": int}`` with exact
        per-file outcomes as described above.

    Raises:
        ValueError: If ``ttl_s`` is not a non-negative number.
    """
    if (
        isinstance(ttl_s, bool)
        or not isinstance(ttl_s, (int, float))
        or not ttl_s >= 0
    ):
        raise ValueError("ttl_s must be a non-negative number of seconds")
    moment = time.time() if now is None else float(now)
    counts = {"expired": 0, "orphans": 0, "kept": 0}
    base = Path(review_dir)
    if not base.is_dir():
        return counts
    try:
        files = sorted(base.glob("*.png"))
    except OSError as exc:
        _logger.warning("review expiry scan failed dir=%s error=%s", str(base), exc)
        return counts
    for path in files:
        try:
            stem = path.stem
            try:
                validate_job_id(stem)
            except ValueError:
                _logger.warning(
                    "review expiry: unexpected file %r kept (not a review job id)",
                    path.name,
                )
                counts["kept"] += 1
                continue
            if not path.is_file():
                # A directory (or special) matching *.png is never a
                # payload; ignore entirely rather than counting it.
                continue
            try:
                row = jobs.get(stem)
            except Exception as exc:
                _logger.warning(
                    "review expiry: job lookup failed job_id=%s error=%s",
                    stem,
                    exc,
                )
                counts["kept"] += 1
                continue
            if row is None:
                if _remove_payload(path, stem):
                    counts["expired"] += 1
                else:
                    counts["kept"] += 1
                continue
            status = row.status
            if status in _TERMINAL_STATUSES:
                if _remove_payload(path, stem):
                    counts["orphans"] += 1
                else:
                    counts["kept"] += 1
                continue
            if status != "pending_review":
                _logger.warning(
                    "review expiry: unexpected status %r job_id=%s kept",
                    status,
                    stem,
                )
                counts["kept"] += 1
                continue
            created = _created_epoch(row.created_at)
            if created is None:
                _logger.warning(
                    "review expiry: unparseable created_at job_id=%s kept",
                    stem,
                )
                counts["kept"] += 1
                continue
            if moment - created > ttl_s:
                row.status = "failed"
                row.error_code = EXPIRED_ERROR_CODE
                row.error_message = EXPIRED_ERROR_MESSAGE
                try:
                    jobs.update(row)
                except Exception as exc:
                    _logger.warning(
                        "review expiry: job update failed job_id=%s error=%s",
                        stem,
                        exc,
                    )
                    counts["kept"] += 1
                    continue
                if _remove_payload(path, stem):
                    counts["expired"] += 1
                else:
                    counts["kept"] += 1
            else:
                counts["kept"] += 1
        except Exception as exc:  # noqa: BLE001 - per-file isolation backstop
            _logger.warning(
                "review expiry: skipped file=%s error=%s", path.name, exc
            )
            counts["kept"] += 1
    return counts
