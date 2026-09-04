"""ComfyUI adapter: Telegram Send Image node (T009).

Node-layer only: adapts ComfyUI inputs/outputs to application services.
No torch/ComfyUI imports here (passthrough needs no tensor ops) and no
raw HTTP (all network goes through :class:`TelegramClient`).

MVP notes (approved decisions):

- ``account`` / ``destination`` are plain STRING ids. Rich selector
  widgets (account/destination dropdowns) are later-epic work (T051/T052);
  until then users paste the ids from their local config.
- ``wait_for_upload`` is accepted per the node contract, but MVP always
  publishes synchronously (ADR-005: the background queue is Epic 4), so
  the flag currently does not change behavior.
- ``skip_duplicate=True`` is refused loudly with ``ConfigurationError``
  because duplicate detection is planned in T034 (a later epic); silently
  ignoring the flag would pretend a guarantee the MVP cannot keep.
- Every other failure (config, encoding, Telegram API) surfaces as
  ``RuntimeError`` carrying the typed error's actionable message. The bot
  token is never interpolated into messages and the input IMAGE is always
  returned untouched (FR-003 / FR-004).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from publisher_nodes import register
from services.encoder import encode_image
from storage.config import ConfigStore, FileSecretStore
from telegram.client import TelegramClient
from telegram.errors import ConfigurationError
from telegram.logging import get_logger

# Extension dir derived from inside publisher_nodes/ (approved locations).
_EXTENSION_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_PATH = _EXTENSION_DIR / "user_config" / "accounts.json"
_DEFAULT_SECRET_PATH = _EXTENSION_DIR / "secrets" / "tokens.json"

_logger = get_logger(__name__)


def _batch_size(image: Any) -> int | None:
    """Best-effort first-dim size for a batched IMAGE, else None."""
    shape = getattr(image, "shape", None)
    if shape is not None:
        try:
            return int(shape[0])
        except (TypeError, IndexError, ValueError):
            pass
    try:
        return len(image)  # type: ignore[arg-type]
    except TypeError:
        return None


def _first_frame(image: Any) -> Any:
    """Return the first frame of a batch, or the single frame itself.

    Batch selection is deterministic: always frame 0 (FR-005). Empty
    batches are rejected with an actionable error instead of sending
    nothing or indexing blindly.
    """
    ndim = getattr(image, "ndim", None)
    if ndim is None:
        shape = getattr(image, "shape", None)
        ndim = len(shape) if shape is not None else None
    if ndim != 4:
        # Single HWC frame (or an unknown layout that encode_image will
        # validate and reject with its own actionable message).
        return image
    if _batch_size(image) == 0:
        raise ValueError(
            "empty image batch: IMAGE has 0 frames; nothing to publish."
        )
    return image[0]


class TelegramSendImage:
    """Publish one image to Telegram, passing IMAGE through unchanged."""

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "publish"
    CATEGORY = "Telegram"
    OUTPUT_NODE = True

    def __init__(
        self,
        config_store: ConfigStore | None = None,
        secret_store: FileSecretStore | None = None,
        client_factory: Callable[[str], TelegramClient] | None = None,
    ) -> None:
        # Injectable seams: tests pass tmp-path stores and a
        # fake-transport factory so no ComfyUI, network, or real
        # secrets are ever needed.
        self._config_store = (
            config_store if config_store is not None else ConfigStore(_DEFAULT_CONFIG_PATH)
        )
        self._secret_store = (
            secret_store
            if secret_store is not None
            else FileSecretStore(_DEFAULT_SECRET_PATH)
        )
        self._client_factory = (
            client_factory if client_factory is not None else TelegramClient
        )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE",),
                "account": ("STRING", {"default": ""}),
                "destination": ("STRING", {"default": ""}),
                "caption": ("STRING", {"multiline": True, "default": ""}),
                "format": (["png", "jpeg"],),
                "quality": ("INT", {"default": 90, "min": 1, "max": 100}),
                "protect_content": ("BOOLEAN", {"default": False}),
                "disable_notification": ("BOOLEAN", {"default": False}),
                "skip_duplicate": ("BOOLEAN", {"default": False}),
                "wait_for_upload": ("BOOLEAN", {"default": True}),
            }
        }

    def publish(
        self,
        image: Any,
        account: str,
        destination: str,
        caption: str = "",
        format: str = "png",
        quality: int = 90,
        protect_content: bool = False,
        disable_notification: bool = False,
        skip_duplicate: bool = False,
        wait_for_upload: bool = True,
    ) -> tuple[Any, ...]:
        """Send the first IMAGE frame to Telegram; return IMAGE unchanged."""
        _ = wait_for_upload  # Contract flag; MVP always sends synchronously.
        if skip_duplicate:
            raise ConfigurationError(
                "duplicate detection is planned in T034; "
                "skip_duplicate=True cannot be honored yet, so the publish "
                "was refused instead of silently ignoring duplicates."
            )
        _logger.info(
            "telegram publish start account=%s destination=%s format=%s quality=%s",
            account,
            destination,
            format,
            quality,
        )
        start = time.perf_counter()
        try:
            dest = self._config_store.get_destination(destination)
            acct = self._config_store.get_account(account)
            if dest.account_id != acct.id:
                raise ConfigurationError(
                    f"destination '{destination}' belongs to account "
                    f"'{dest.account_id}', not '{account}'; refusing to "
                    f"publish with the wrong account's token."
                )
            # Local only; never stored on the node, logged, or put in errors.
            token = self._config_store.resolve_token(acct.id, self._secret_store)
            frame = _first_frame(image)
            encoded = encode_image(frame, format=format, quality=quality)
            client = self._client_factory(token)
            client.send_photo(
                dest.chat_id,
                encoded.data,
                f"comfyui_{encoded.width}x{encoded.height}.{encoded.format}",
                caption or None,
                protect_content=bool(protect_content),
                disable_notification=bool(disable_notification),
            )
        except Exception as exc:
            # Decision (a): every failure surfaces as RuntimeError with the
            # typed error's actionable message; the original is chained.
            # The token is never interpolated above, so it cannot leak here.
            message = f"Telegram publish failed: {exc}"
            _logger.error(
                "telegram publish failed account=%s destination=%s error=%s",
                account,
                destination,
                message,
            )
            raise RuntimeError(message) from exc
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _logger.info(
            "telegram publish success account=%s destination=%s elapsed_ms=%.1f",
            account,
            destination,
            elapsed_ms,
        )
        return (image,)


register(TelegramSendImage, "Telegram Send Image", "Telegram Send Image")
