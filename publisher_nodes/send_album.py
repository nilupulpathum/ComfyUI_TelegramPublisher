"""ComfyUI adapter: Telegram Send Album node (T031 + Epic 3 integration).

Node-layer only: adapts ComfyUI inputs/outputs to application services.
No torch/ComfyUI imports here (frame splitting is duck-typed in
:mod:`services.batch`) and no raw HTTP (all network goes through
:class:`TelegramClient`).

Decisions:

- ``images`` is a ComfyUI IMAGE batch; frames come from
  :func:`services.batch.extract_frames` in order (frame 0 first, FR-005).
- Telegram ``sendMediaGroup`` needs 2-10 items: any other frame count is
  a fail-fast ``ValueError`` with an actionable message (use Telegram
  Send Image for single frames). No history is written and no network is
  touched for this validation failure.
- Duplicate detection (``skip_duplicate=True``) checks EVERY frame hash
  via :class:`services.dedup.DuplicateDetector`; the FIRST hit refuses the
  WHOLE album loudly (no partial album is ever sent). The lookup is
  fail-open: a DB failure warns and the publish proceeds.
- ``caption`` is a template rendered with generation metadata (width,
  height, and filename come from the FIRST encoded frame); the rendered
  text goes on the first album item only (Telegram behavior).
- ``protect_content`` / ``disable_notification`` / ``wait_for_upload``
  are accepted for contract parity with Send Image, but the current
  ``send_media_group`` wrapper does not expose per-send flags, so they
  currently do not change behavior (like ``wait_for_upload`` on Send
  Image, which always sends synchronously per ADR-005).
- History (one success ``publish_jobs`` row with the first-frame hash and
  comma-joined message ids, plus one ``generations`` row) is lazy and
  best-effort: any history failure logs a warning and the publish still
  succeeds (history NEVER blocks publishing).
- Every send-path failure surfaces as ``RuntimeError`` carrying the typed
  error's actionable message (chained). The bot token is never
  interpolated into messages and the input IMAGE batch is always returned
  untouched (FR-003 / FR-004).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from publisher_nodes import register
from publisher_nodes.send_image import (
    _history_repos,
    _record_generation,
    _DEFAULT_CONFIG_PATH,
    _DEFAULT_HISTORY_PATH,
    _DEFAULT_SECRET_PATH,
    OPTIONAL_METADATA_INPUTS,
)
from services.batch import extract_frames
from services.captions import render_caption
from services.dedup import DuplicateDetector, sha256_hex
from services.encoder import encode_image
from services.metadata import GenerationMetadata
from storage.config import ConfigStore, FileSecretStore
from storage.repositories import PublishJob
from telegram.client import TelegramClient
from telegram.errors import ConfigurationError, DuplicateError
from telegram.logging import get_logger

_logger = get_logger(__name__)


class TelegramSendAlbum:
    """Publish an IMAGE batch as a Telegram album; pass IMAGE through."""

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "publish"
    CATEGORY = "Telegram"
    OUTPUT_NODE = True

    def __init__(
        self,
        config_store: ConfigStore | None = None,
        secret_store: FileSecretStore | None = None,
        client_factory: Callable[[str], TelegramClient] | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        # Injectable seams: tests pass tmp-path stores, a fake-transport
        # factory, and a tmp db_path so no ComfyUI, network, real secrets,
        # or repo files are ever needed.
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
        self._db_path = Path(db_path) if db_path is not None else _DEFAULT_HISTORY_PATH

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "images": ("IMAGE",),
                "account": ("STRING", {"default": ""}),
                "destination": ("STRING", {"default": ""}),
                "caption": ("STRING", {"multiline": True, "default": ""}),
                "format": (["png", "jpeg"],),
                "quality": ("INT", {"default": 90, "min": 1, "max": 100}),
                "protect_content": ("BOOLEAN", {"default": False}),
                "disable_notification": ("BOOLEAN", {"default": False}),
                "skip_duplicate": ("BOOLEAN", {"default": False}),
                "wait_for_upload": ("BOOLEAN", {"default": True}),
            },
            "optional": dict(OPTIONAL_METADATA_INPUTS),
        }

    def publish(
        self,
        images: Any,
        account: str,
        destination: str,
        caption: str = "",
        format: str = "png",
        quality: int = 90,
        protect_content: bool = False,
        disable_notification: bool = False,
        skip_duplicate: bool = False,
        wait_for_upload: bool = True,
        prompt: str = "",
        negative_prompt: str = "",
        seed: str = "",
        steps: str = "",
        cfg: str = "",
        sampler: str = "",
        scheduler: str = "",
        model: str = "",
    ) -> tuple[Any, ...]:
        """Send IMAGE frames as a Telegram media group; return IMAGE unchanged."""
        _ = wait_for_upload  # Contract flag; always sends synchronously.
        _ = protect_content  # Accepted for contract parity; not forwarded
        _ = disable_notification  # by the send_media_group wrapper (see docs).
        # Fail-fast validation: no history, no network on bad input.
        frames = extract_frames(images)
        count = len(frames)
        if count < 2 or count > 10:
            raise ValueError(
                f"sendMediaGroup needs 2-10 items; got {count} — use "
                "Telegram Send Image for single frames."
            )
        _logger.info(
            "telegram album publish start account=%s destination=%s frames=%s format=%s quality=%s",
            account,
            destination,
            count,
            format,
            quality,
        )
        start = time.perf_counter()
        # Best-effort history fields, filled as the publish proceeds.
        hashes: list[str] = []
        filename: str | None = None
        caption_text = ""
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
            encoded_frames = [
                encode_image(frame, format=format, quality=quality)
                for frame in frames
            ]
            first = encoded_frames[0]
            filename = f"comfyui_{first.width}x{first.height}.{first.format}"
            metadata = GenerationMetadata.from_mapping(
                {
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "seed": seed,
                    "steps": steps,
                    "cfg": cfg,
                    "sampler": sampler,
                    "scheduler": scheduler,
                    "model": model,
                    "width": first.width,
                    "height": first.height,
                    "filename": filename,
                }
            )
            rendered = render_caption(caption, metadata.as_template_vars())
            caption_text = rendered.text
            for warning in rendered.warnings:
                _logger.warning("telegram caption warning: %s", warning)
            hashes = [sha256_hex(item.data) for item in encoded_frames]
            if skip_duplicate:
                # First hit refuses the WHOLE album: no partial send.
                # Fail-open on DB trouble (warn + proceed); only a
                # positive DuplicateError refuses.
                repos = _history_repos(self._db_path)
                if repos is not None:
                    job_repo, _ = repos
                    detector = DuplicateDetector(job_repo)
                    try:
                        for image_hash in hashes:
                            detector.check(image_hash, dest.id)
                    except DuplicateError:
                        raise
                    except Exception as exc:
                        _logger.warning(
                            "telegram duplicate check failed; proceeding: %s",
                            exc,
                        )
            client = self._client_factory(token)
            message_ids = client.send_media_group(
                dest.chat_id,
                [(item.data, filename) for item in encoded_frames],
                caption_text or None,
            )
            try:
                repos = _history_repos(self._db_path)
                if repos is not None:
                    job_repo, gen_repo = repos
                    job = job_repo.create(
                        PublishJob(
                            destination_id=dest.id,
                            status="success",
                            image_hash=hashes[0],
                            filename=filename,
                            caption=caption_text,
                            attempts=1,
                            telegram_message_id=",".join(
                                str(mid) for mid in message_ids
                            ),
                        )
                    )
                    _record_generation(gen_repo, str(job.id), metadata)
            except Exception as exc:
                _logger.warning("telegram history write failed: %s", exc)
        except Exception as exc:
            # Every send-path failure surfaces as RuntimeError with the
            # typed error's actionable message; the original is chained.
            # The token is never interpolated above, so it cannot leak here.
            try:
                repos = _history_repos(self._db_path)
                if repos is not None:
                    job_repo, _ = repos
                    job_repo.create(
                        PublishJob(
                            destination_id=destination,
                            status="failed",
                            image_hash=hashes[0] if hashes else None,
                            filename=filename,
                            caption=caption_text,
                            attempts=1,
                            error_code=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
            except Exception as history_exc:
                _logger.warning("telegram history write failed: %s", history_exc)
            message = f"Telegram publish failed: {exc}"
            _logger.error(
                "telegram album publish failed account=%s destination=%s error=%s",
                account,
                destination,
                message,
            )
            raise RuntimeError(message) from exc
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _logger.info(
            "telegram album publish success account=%s destination=%s frames=%s elapsed_ms=%.1f",
            account,
            destination,
            count,
            elapsed_ms,
        )
        return (images,)


register(TelegramSendAlbum, "Telegram Send Album", "Telegram Send Album")
