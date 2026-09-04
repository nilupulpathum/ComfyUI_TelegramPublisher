"""Unit tests for services.encoder (backlog T008)."""

import io

import numpy as np
import pytest
from PIL import Image

from services.encoder import EncodedImage, encode_image

try:
    import torch  # noqa: F401
except ImportError:
    torch = None


def _rgb_frame(h: int = 8, w: int = 6) -> np.ndarray:
    rng = np.random.default_rng(42)
    return rng.random((h, w, 3), dtype=np.float32)


def test_rgb_png_magic_bytes_and_metadata() -> None:
    frame = _rgb_frame()
    result = encode_image(frame, format="png")
    assert isinstance(result, EncodedImage)
    assert result.format == "png"
    assert result.content_type == "image/png"
    assert result.width == 6
    assert result.height == 8
    assert result.data[:4] == bytes([0x89, 0x50, 0x4E, 0x47])
    decoded = Image.open(io.BytesIO(result.data))
    assert decoded.size == (6, 8)
    assert decoded.mode == "RGB"


def test_rgba_png_preserves_alpha() -> None:
    frame = _rgb_frame()
    alpha = np.full((8, 6, 1), 0.5, dtype=np.float32)
    rgba = np.concatenate([frame, alpha], axis=2)
    result = encode_image(rgba, format="png")
    assert result.format == "png"
    assert result.content_type == "image/png"
    decoded = Image.open(io.BytesIO(result.data))
    assert decoded.mode == "RGBA"
    _, _, _, decoded_alpha = decoded.split()
    assert 120 <= int(np.asarray(decoded_alpha).mean()) <= 136


def test_jpeg_content_type_and_magic_bytes() -> None:
    frame = _rgb_frame()
    result = encode_image(frame, format="jpeg")
    assert result.format == "jpeg"
    assert result.content_type == "image/jpeg"
    assert result.data[:2] == bytes([0xFF, 0xD8])
    decoded = Image.open(io.BytesIO(result.data))
    assert decoded.size == (6, 8)


def test_jpeg_drops_alpha() -> None:
    frame = _rgb_frame()
    rgba = np.concatenate(
        [frame, np.full((8, 6, 1), 0.25, dtype=np.float32)], axis=2
    )
    result = encode_image(rgba, format="jpeg")
    assert result.content_type == "image/jpeg"
    assert result.data[:2] == bytes([0xFF, 0xD8])
    decoded = Image.open(io.BytesIO(result.data))
    assert decoded.mode in ("RGB", "L")


def test_quality_bounds_rejected() -> None:
    frame = _rgb_frame()
    with pytest.raises(ValueError):
        encode_image(frame, format="jpeg", quality=0)
    with pytest.raises(ValueError):
        encode_image(frame, format="jpeg", quality=101)


def test_quality_changes_jpeg_byte_size() -> None:
    rng = np.random.default_rng(7)
    frame = rng.random((64, 64, 3), dtype=np.float32)
    small = encode_image(frame, format="jpeg", quality=10)
    large = encode_image(frame, format="jpeg", quality=95)
    assert len(small.data) < len(large.data)


def test_unknown_format_rejected() -> None:
    with pytest.raises(ValueError):
        encode_image(_rgb_frame(), format="bmp")


@pytest.mark.parametrize("shape", [(8, 6), (2, 8, 6, 3)])
def test_invalid_shapes_rejected(shape: tuple[int, ...]) -> None:
    arr = np.zeros(shape, dtype=np.float32)
    with pytest.raises(ValueError):
        encode_image(arr)


@pytest.mark.parametrize("channels", [2, 5])
def test_bad_channel_count_rejected(channels: int) -> None:
    arr = np.zeros((8, 6, channels), dtype=np.float32)
    with pytest.raises(ValueError):
        encode_image(arr)


def test_nan_rejected() -> None:
    frame = _rgb_frame()
    frame[0, 0, 0] = float("nan")
    with pytest.raises(ValueError):
        encode_image(frame)


def test_inf_rejected() -> None:
    frame = _rgb_frame()
    frame[1, 1, 1] = float("inf")
    with pytest.raises(ValueError):
        encode_image(frame)


def test_empty_dims_rejected() -> None:
    arr = np.zeros((0, 6, 3), dtype=np.float32)
    with pytest.raises(ValueError):
        encode_image(arr)


def test_does_not_mutate_numpy_input() -> None:
    frame = _rgb_frame()
    before = frame.copy()
    encode_image(frame, format="png")
    encode_image(frame, format="jpeg", quality=75)
    assert np.array_equal(frame, before)


@pytest.mark.skipif(torch is None, reason="torch not installed")
def test_torch_tensor_input() -> None:
    frame = torch.rand(8, 6, 3, dtype=torch.float32)
    before = frame.clone()
    result = encode_image(frame, format="png")
    assert result.data[:4] == bytes([0x89, 0x50, 0x4E, 0x47])
    assert result.width == 6
    assert result.height == 8
    assert torch.equal(frame, before)


@pytest.mark.skipif(torch is None, reason="torch not installed")
def test_torch_grayscale_input() -> None:
    frame = torch.rand(4, 5, 1, dtype=torch.float32)
    result = encode_image(frame, format="png")
    assert result.width == 5
    assert result.height == 4
    decoded = Image.open(io.BytesIO(result.data))
    assert decoded.mode == "L"


def test_grayscale_numpy_input() -> None:
    rng = np.random.default_rng(3)
    frame = rng.random((4, 5, 1), dtype=np.float32)
    result = encode_image(frame, format="png")
    assert result.width == 5
    assert result.height == 4
    decoded = Image.open(io.BytesIO(result.data))
    assert decoded.mode == "L"
