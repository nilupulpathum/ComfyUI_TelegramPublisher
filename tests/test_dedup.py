"""T034: DuplicateDetector unit tests. No sqlite, no network, no tokens."""

import pytest

from services.dedup import DuplicateDetector, sha256_hex
from telegram.errors import DuplicateError


class FakeJobRepo:
    """Tiny in-memory fake: stores (hash, destination, status) triples."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, str, str]] = []

    def add(self, image_hash: str, destination_id: str, status: str) -> None:
        self.jobs.append((image_hash, destination_id, status))

    def find_by_hash(
        self,
        image_hash: str,
        destination_id: str,
        statuses: tuple[str, ...] = ("success",),
    ):
        for h, d, s in self.jobs:
            if h == image_hash and d == destination_id and s in statuses:
                return {"image_hash": h, "destination_id": d, "status": s}
        return None


# -- sha256_hex ------------------------------------------------------------


def test_sha256_known_vector():
    assert (
        sha256_hex(b"abc")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_sha256_rejects_empty():
    with pytest.raises(ValueError):
        sha256_hex(b"")


def test_sha256_rejects_non_bytes():
    for bad in ("abc", None, 123, bytearray(b"abc")):
        with pytest.raises(ValueError):
            sha256_hex(bad)


def test_sha256_differs_per_payload():
    assert sha256_hex(b"a") != sha256_hex(b"b")


# -- DuplicateDetector.check -----------------------------------------------


def test_hit_same_destination_raises():
    repo = FakeJobRepo()
    repo.add("deadbeef", "chan-1", "success")
    detector = DuplicateDetector(repo)
    with pytest.raises(DuplicateError) as exc_info:
        detector.check("deadbeef", "chan-1")
    message = str(exc_info.value)
    assert "chan-1" in message
    assert "refus" in message.lower()  # re-publish was refused


def test_same_image_different_destination_no_hit():
    repo = FakeJobRepo()
    repo.add("deadbeef", "chan-1", "success")
    DuplicateDetector(repo).check("deadbeef", "chan-2")  # must not raise


def test_failed_jobs_do_not_block():
    repo = FakeJobRepo()
    repo.add("deadbeef", "chan-1", "failed")
    DuplicateDetector(repo).check("deadbeef", "chan-1")  # must not raise


def test_pending_jobs_do_not_block():
    repo = FakeJobRepo()
    repo.add("deadbeef", "chan-1", "pending")
    DuplicateDetector(repo).check("deadbeef", "chan-1")  # must not raise


def test_disabled_path_is_simply_not_calling_check():
    """FR-006: detection is optional; skipping check() never raises."""

    class ExplodingRepo(FakeJobRepo):
        def find_by_hash(self, *args, **kwargs):
            raise AssertionError("check must not be consulted when disabled")

    DuplicateDetector(ExplodingRepo())  # constructing + never calling is the path


def test_empty_inputs_rejected():
    detector = DuplicateDetector(FakeJobRepo())
    for bad_hash, bad_dest in (("", "chan-1"), ("  ", "chan-1"), (None, "chan-1")):
        with pytest.raises(ValueError):
            detector.check(bad_hash, "chan-1")
    for bad_dest in ("", "  ", None, 123):
        with pytest.raises(ValueError):
            detector.check("deadbeef", bad_dest)
