"""Duplicate detector (T034).

Exact-upload duplicate detection: SHA-256 of the exact encoded image
payload, scoped to destination (FR-006, TECHNICAL_SPEC section 5).

Duplicate detection is optional: callers skip duplicates by simply not
calling :meth:`DuplicateDetector.check` (e.g. when a ``skip_duplicate``
node input is false). There is no enabled flag in this module by design.

Only dependency outside this module is :class:`telegram.errors.DuplicateError`
(classification-only; no HTTP or ComfyUI imports).
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from telegram.errors import DuplicateError


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of the exact encoded payload.

    Raises:
        ValueError: If ``data`` is not ``bytes`` or is empty.
    """
    if not isinstance(data, bytes) or not data:
        raise ValueError("data must be non-empty bytes")
    return hashlib.sha256(data).hexdigest()


class JobRepository(Protocol):
    """Duck-typed repository surface needed by :class:`DuplicateDetector`."""

    def find_by_hash(
        self,
        image_hash: str,
        destination_id: str,
        statuses: tuple[str, ...] = ("success",),
    ) -> Any | None: ...


class DuplicateDetector:
    """Refuse re-publish of an image already published to a destination.

    Args:
        job_repo: A ``PublishJobRepository`` (duck-typed: needs
            ``find_by_hash(image_hash, destination_id, statuses)``).
    """

    def __init__(self, job_repo: JobRepository) -> None:
        self._job_repo = job_repo

    def check(self, image_hash: str, destination_id: str) -> None:
        """Raise :class:`DuplicateError` if this hash already succeeded.

        Lookup is scoped to the given destination: the same image sent to
        a *different* destination is not a duplicate. Only jobs whose
        status is ``"success"`` block re-publish; failed/pending jobs do
        not.

        Raises:
            ValueError: If ``image_hash`` or ``destination_id`` is empty.
            DuplicateError: If the image was already published there.
        """
        if not isinstance(image_hash, str) or not image_hash.strip():
            raise ValueError("image_hash must be a non-empty string")
        if not isinstance(destination_id, str) or not destination_id.strip():
            raise ValueError("destination_id must be a non-empty string")
        hit = self._job_repo.find_by_hash(image_hash, destination_id, ("success",))
        if hit is not None:
            raise DuplicateError(
                f"duplicate image (hash {image_hash}) already published "
                f"to destination '{destination_id}'; re-publish refused"
            )
