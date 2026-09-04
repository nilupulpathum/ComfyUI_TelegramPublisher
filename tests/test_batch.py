"""T030 tests: batch frame extraction. Numpy + torch, stdlib only otherwise."""

from __future__ import annotations

import numpy as np
import pytest
import torch

from services.batch import extract_frames


def test_batch_order_preserved_numpy():
    batch = np.zeros((3, 4, 5, 3), dtype=np.float32)
    for i in range(3):
        batch[i] = float(i) / 10.0
    frames = extract_frames(batch)
    assert len(frames) == 3
    for i, frame in enumerate(frames):
        assert frame.shape == (4, 5, 3)
        assert np.array_equal(frame, batch[i])
        assert float(frame[0, 0, 0]) == pytest.approx(float(i) / 10.0)


def test_batch_order_preserved_torch():
    batch = torch.zeros(3, 4, 5, 3)
    for i in range(3):
        batch[i] = float(i) / 10.0
    frames = extract_frames(batch)
    assert len(frames) == 3
    for i, frame in enumerate(frames):
        assert tuple(frame.shape) == (4, 5, 3)
        assert torch.equal(frame, batch[i])


def test_single_frame_passthrough_numpy():
    single = np.zeros((4, 5, 3), dtype=np.float32)
    frames = extract_frames(single)
    assert len(frames) == 1
    assert frames[0] is single


def test_single_frame_passthrough_torch():
    single = torch.zeros(4, 5, 3)
    frames = extract_frames(single)
    assert len(frames) == 1
    assert frames[0] is single


def test_two_dimensional_rejected():
    with pytest.raises(ValueError):
        extract_frames(np.zeros((8, 8), dtype=np.float32))


def test_five_dimensional_rejected():
    with pytest.raises(ValueError):
        extract_frames(np.zeros((2, 8, 8, 3, 1), dtype=np.float32))


def test_empty_batch_rejected():
    with pytest.raises(ValueError, match="empty image batch"):
        extract_frames(np.zeros((0, 8, 8, 3), dtype=np.float32))


def test_empty_batch_rejected_torch():
    with pytest.raises(ValueError, match="empty image batch"):
        extract_frames(torch.zeros(0, 8, 8, 3))


def test_no_shape_rejected():
    with pytest.raises(ValueError):
        extract_frames(object())
