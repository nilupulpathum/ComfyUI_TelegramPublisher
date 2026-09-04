"""Image encoder: ComfyUI IMAGE frame -> PNG/JPEG bytes.

Input is ONE ComfyUI IMAGE frame: 3D HWC float32 in [0,1] with
C in {1, 3, 4}. Accepts torch tensors and numpy arrays (and anything
convertible via ``numpy.asarray``) without importing torch.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class EncodedImage:
    data: bytes
    format: str  # "png" | "jpeg"
    width: int
    height: int
    content_type: str  # "image/png" | "image/jpeg"


def _to_numpy(image: Any) -> np.ndarray:
    """Move a duck-typed tensor/array to a numpy array on CPU.

    Handles torch tensors via ``detach()``/``cpu()``/``numpy()`` when
    those attributes exist, otherwise falls back to ``numpy.asarray``.
    Never mutates the caller's object.
    """
    obj: Any = image
    detach = getattr(obj, "detach", None)
    if callable(detach):
        obj = detach()
    cpu = getattr(obj, "cpu", None)
    if callable(cpu):
        obj = cpu()
    to_numpy = getattr(obj, "numpy", None)
    if callable(to_numpy):
        obj = to_numpy()
    try:
        arr = np.asarray(obj)
    except Exception as exc:
        raise ValueError(
            "encode_image expected an HWC float32 image array convertible "
            f"via numpy.asarray; conversion failed: {exc}"
        ) from exc
    return arr


def encode_image(image: Any, *, format: str = "png", quality: int = 90) -> EncodedImage:
    """Encode one ComfyUI IMAGE frame to PNG or JPEG bytes.

    Args:
        image: Single frame, 3D HWC with channels in {1, 3, 4},
            float values in [0, 1]. torch Tensor or numpy array.
        format: "png" or "jpeg" (case-insensitive; "jpg" accepted
            as an alias for "jpeg").
        quality: JPEG quality in 1..100 (ignored for PNG).

    Returns:
        EncodedImage with bytes plus width/height/content metadata.

    Raises:
        ValueError: On invalid shape, channels, non-finite pixels,
            empty dims, unknown format, or out-of-range quality.
    """
    normalized = (format or "").strip().lower()
    if normalized == "jpg":
        normalized = "jpeg"
    if normalized not in ("png", "jpeg"):
        raise ValueError(
            f"Unknown image format {format!r}; expected 'png' or 'jpeg'."
        )
    if not isinstance(quality, int) or isinstance(quality, bool):
        raise ValueError(
            f"JPEG quality must be an int in 1..100; got {quality!r}."
        )
    if quality < 1 or quality > 100:
        raise ValueError(
            f"JPEG quality must be in 1..100; got {quality}."
        )

    arr = _to_numpy(image)

    if arr.ndim != 3:
        raise ValueError(
            "Invalid image dimensions: expected a single 3D HWC frame with "
            f"shape (H, W, C); got {arr.ndim}D array with shape {arr.shape}."
        )
    height, width, channels = arr.shape
    if height == 0 or width == 0 or channels == 0:
        raise ValueError(
            "Invalid image dimensions: H, W, and C must all be non-zero; "
            f"got shape {arr.shape}."
        )
    if arr.size == 0:
        raise ValueError(
            f"Invalid image: empty array with shape {arr.shape}."
        )
    if channels not in (1, 3, 4):
        raise ValueError(
            "Invalid channel count: expected 1 (L), 3 (RGB), or 4 (RGBA); "
            f"got {channels} with shape {arr.shape}."
        )
    try:
        finite = bool(np.isfinite(arr).all())
    except TypeError as exc:
        raise ValueError(
            "Invalid image pixels: array must contain real numbers; "
            f"got dtype {arr.dtype}."
        ) from exc
    if not finite:
        raise ValueError(
            "Invalid image pixels: NaN or inf values are not allowed."
        )

    # Out-of-place ops only: never mutate the caller's array.
    clipped = np.clip(arr, 0.0, 1.0)
    as_uint8 = np.round(clipped * 255.0).astype(np.uint8)

    if channels == 1:
        pil_image = Image.fromarray(as_uint8[:, :, 0], mode="L")
    elif channels == 3:
        pil_image = Image.fromarray(as_uint8, mode="RGB")
    else:
        pil_image = Image.fromarray(as_uint8, mode="RGBA")

    if normalized == "jpeg" and pil_image.mode == "RGBA":
        # Drop alpha over a white background.
        background = Image.new("RGB", pil_image.size, (255, 255, 255))
        background.paste(pil_image, mask=pil_image.split()[3])
        pil_image = background
    elif normalized == "jpeg" and pil_image.mode not in ("RGB", "L"):
        pil_image = pil_image.convert("RGB")

    buffer = io.BytesIO()
    if normalized == "png":
        pil_image.save(buffer, format="PNG")
        content_type = "image/png"
    else:
        save_image = pil_image
        if save_image.mode == "RGBA":
            save_image = save_image.convert("RGB")
        save_image.save(buffer, format="JPEG", quality=quality)
        content_type = "image/jpeg"

    return EncodedImage(
        data=buffer.getvalue(),
        format=normalized,
        width=width,
        height=height,
        content_type=content_type,
    )
