"""ComfyUI node adapters (T009, T031). No raw HTTP here.

Each node module registers its class via :func:`register` so the
extension entry point (root ``__init__.py``) has a single source for
``NODE_CLASS_MAPPINGS`` / ``NODE_DISPLAY_NAME_MAPPINGS``.

Registration failures are loud on purpose: a duplicate or invalid entry
means a packaging bug, and ComfyUI must not silently load a half-wired
extension.
"""

NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}


def register(node_class: type, node_id: str, display_name: str) -> None:
    """Register one node class under a unique ComfyUI node id."""
    if not isinstance(node_id, str) or not node_id:
        raise ValueError(f"node_id must be a non-empty string, got {node_id!r}.")
    if not isinstance(node_class, type):
        raise ValueError(
            f"node_class for {node_id!r} must be a class, "
            f"got {type(node_class).__name__}."
        )
    if not isinstance(display_name, str) or not display_name:
        raise ValueError(
            f"display_name for {node_id!r} must be a non-empty string."
        )
    if node_id in NODE_CLASS_MAPPINGS:
        raise ValueError(f"duplicate node_id registered: {node_id!r}.")
    NODE_CLASS_MAPPINGS[node_id] = node_class
    NODE_DISPLAY_NAME_MAPPINGS[node_id] = display_name


__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "register",
    "TelegramSendImage",
]


# Explicit node imports so registration runs on package import.
# Failures are loud by design (see module docstring).
from .send_image import TelegramSendImage  # noqa: E402,F401
