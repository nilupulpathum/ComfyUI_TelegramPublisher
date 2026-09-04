"""Pending-review image payload store (T063).

Filesystem companion to the ``pending_review`` publish-job rows: while a
generation awaits admin approval, its first-frame PNG bytes live ONLY
under the review directory as ``<jobid>.png``. Stdlib only; no ComfyUI,
no network, no secrets.

Security properties:

- Job ids are validated against ``[A-Za-z0-9_-]{1,64}`` BEFORE any
  filesystem use, so a hostile ``/approve ../../x`` argument can never
  escape the review directory (path traversal guard).
- Payloads must be non-empty, PNG-magic-checked (``89 50 4E 47``), and
  ``<= 10 MB``; anything else raises ``ValueError`` and is never stored.
- Missing payloads raise ``ValueError`` with a clear ``"review payload
  missing"`` message (kept simple: no ``FileNotFoundError`` subclass).
"""

from __future__ import annotations

import re
from pathlib import Path

#: Directory name holding review payloads next to the history database.
DEFAULT_REVIEW_DIRNAME = "review"

#: Max accepted review payload size (10 MB).
MAX_REVIEW_BYTES = 10 * 1024 * 1024

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
