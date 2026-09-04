"""Batch frame extraction (T030).

Splits a ComfyUI IMAGE batch into an ordered list of single frames.
Pure stdlib + duck-typing: works on torch tensors and numpy arrays via
``shape``/``len`` without importing torch. Indexing ``image[i]`` works
for both, and frame 0 always comes first (deterministic, FR-005).
"""

from __future__ import annotations

from typing import Any


def extract_frames(image: Any) -> list[Any]:
    """Split an IMAGE batch into a list of frames in order.

    - 4D batch ``(B, H, W, C)`` -> list of ``B`` HWC frames, frame 0 first.
    - 3D single HWC frame -> ``[image]`` (passthrough, same object).
    - Anything else (2D, 5D, no ``shape``) -> ``ValueError``.
    - Empty batch (dim 0 == 0) -> ``ValueError`` ("empty image batch...").

    Never copies pixel data: batch frames are views/references into the
    original object (``image[i]``), and single frames pass through untouched.
    """
    shape = getattr(image, "shape", None)
    if shape is None:
        raise ValueError(
            "invalid image: expected a 4D IMAGE batch (B, H, W, C) or a "
            "single 3D HWC frame, but the object has no shape."
        )
    try:
        ndim = len(shape)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(
            "invalid image: expected a 4D IMAGE batch (B, H, W, C) or a "
            f"single 3D HWC frame; got an unreadable shape {shape!r}."
        ) from exc
    if ndim == 3:
        return [image]
    if ndim != 4:
        raise ValueError(
            "invalid image dimensions: expected a 4D IMAGE batch (B, H, W, C) "
            f"or a single 3D HWC frame; got a {ndim}D array with shape "
            f"{tuple(shape)}."
        )
    try:
        count = int(shape[0])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(
            "invalid image batch: unreadable batch size from shape "
            f"{tuple(shape)}."
        ) from exc
    if count <= 0:
        raise ValueError(
            "empty image batch: IMAGE has 0 frames; nothing to publish."
        )
    return [image[i] for i in range(count)]
