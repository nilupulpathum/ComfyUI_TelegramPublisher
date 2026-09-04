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
- ``protect_content`` / ``disable_notification`` are accepted for
  contract parity with Send Image, but the ``sendMediaGroup`` wrapper
  does not expose per-send flags, so they are carried on the payload
  and ignored at send time (both paths).
- ``wait_for_upload=True`` (default) publishes synchronously as before.
  ``wait_for_upload=False`` runs the SAME pre-flight (frame-count
  validation, config resolution, duplicate check, encoding, caption
  rendering, hashing) and then enqueues an ``album`` payload on the
  process-wide shared queue, returning the input batch immediately
  without network. The background worker uploads later and moves the
  persisted ``publish_jobs`` row to ``success``/``failed``. Validation,
  duplicate, and caption errors still raise synchronously on the
  background path (fail fast before enqueue).
- Review mode (``settings.review_mode``, T063, OFF by default): when
  enabled, the node runs the same pre-flight, stores the FIRST frame's
  PNG bytes as the review payload, records a ``pending_review`` job row
  plus a generation row (best-effort history), notifies each admin via
  ``send_message`` (best-effort), and returns the input batch WITHOUT
  sending media. Review takes precedence over ``wait_for_upload=False``
  (ignored with an info log: review is inherently deferred). A
  settings-read failure warns and falls back to NORMAL publish
  (fail-open).
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
    _account_options,
    _combo_options,
    _default_shared_queue,
    _destination_options,
    _history_repos,
    _record_generation,
    _review_enabled,
    _store_review,
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
from services.queue import PublishPayload, PublishQueue
from storage.config import ConfigStore, FileSecretStore
from storage.repositories import PublishJob
from telegram.client import TelegramClient
from telegram.errors import ConfigurationError, DuplicateError
from telegram.friendly import friendly_message
from telegram.logging import get_logger

_logger = get_logger(__name__)


def _make_sender(
    config_store: ConfigStore, secret_store: FileSecretStore
) -> Callable[[PublishPayload], str]:
    """Build the queue worker's sender: fresh token per job, one attempt.

    Kept in this module (mirroring ``send_image._make_sender``) so node
    dependencies stay explicit. The token is resolved FRESH on every
    call (rotation-safe) and stays local: never stored, logged, or in
    error text. Single attempt (``retry_policy=None``): the worker owns
    retries (FR-008). Album dispatch uses ``send_media_group`` (caption
    on the first item via the client); per-send ``protect_content`` /
    ``disable_notification`` flags are not forwarded by that wrapper.
    """

    def sender(payload: PublishPayload) -> str:
        token = config_store.resolve_token(payload.account_id, secret_store)
        client = TelegramClient(token, retry_policy=None)
        if payload.kind == "photo":
            data, filename = payload.files[0]
            result = client.send_photo(
                payload.chat_id,
                bytes(data),
                filename,
                payload.caption or None,
                protect_content=bool(payload.protect_content),
                disable_notification=bool(payload.disable_notification),
            )
            return str(result.message_id)
        if payload.kind == "album":
            message_ids = client.send_media_group(
                payload.chat_id,
                [
                    (bytes(item_data), item_name)
                    for item_data, item_name in payload.files
                ],
                payload.caption or None,
            )
            return ",".join(str(mid) for mid in message_ids)
        raise ValueError(f"unknown payload kind {payload.kind!r}")

    return sender


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
        queue_factory: Callable[[], PublishQueue] | None = None,
    ) -> None:
        # Injectable seams: tests pass tmp-path stores, a fake-transport
        # factory, a tmp db_path, and a fake queue_factory so no ComfyUI,
        # network, real secrets, or repo files are ever needed.
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
        if queue_factory is not None:
            if not callable(queue_factory):
                raise ValueError("queue_factory must be callable or None")
            self._queue_factory = queue_factory
        else:
            def _shared() -> PublishQueue:
                return _default_shared_queue(
                    self._config_store, self._secret_store, self._db_path
                )

            self._queue_factory = _shared

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "images": ("IMAGE",),
                "account": _account_options(),
                "destination": _destination_options(),
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
        # Fail-fast validation: no history, no network on bad input.
        frames = extract_frames(images)
        count = len(frames)
        if count < 2 or count > 10:
            raise ValueError(
                f"sendMediaGroup needs 2-10 items; got {count} — use "
                "Telegram Send Image for single frames."
            )
        review_on, admin_ids = _review_enabled(self._config_store)
        if review_on:
            return self._publish_review(
                frames,
                images,
                account,
                destination,
                caption=caption,
                format=format,
                quality=quality,
                skip_duplicate=skip_duplicate,
                wait_for_upload=wait_for_upload,
                admin_ids=admin_ids,
                prompt=prompt,
                negative_prompt=negative_prompt,
                seed=seed,
                steps=steps,
                cfg=cfg,
                sampler=sampler,
                scheduler=scheduler,
                model=model,
            )
        if not wait_for_upload:
            return self._publish_background(
                frames,
                images,
                account,
                destination,
                caption=caption,
                format=format,
                quality=quality,
                protect_content=protect_content,
                disable_notification=disable_notification,
                skip_duplicate=skip_duplicate,
                prompt=prompt,
                negative_prompt=negative_prompt,
                seed=seed,
                steps=steps,
                cfg=cfg,
                sampler=sampler,
                scheduler=scheduler,
                model=model,
            )
        _ = protect_content  # Accepted for contract parity; not forwarded
        _ = disable_notification  # by the send_media_group wrapper (see docs).
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
            message = f"Telegram publish failed: {friendly_message(exc, action='publish')}"
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

    def _publish_review(
        self,
        frames: list[Any],
        images: Any,
        account: str,
        destination: str,
        caption: str = "",
        format: str = "png",
        quality: int = 90,
        skip_duplicate: bool = False,
        wait_for_upload: bool = True,
        admin_ids: tuple[str, ...] = (),
        prompt: str = "",
        negative_prompt: str = "",
        seed: str = "",
        steps: str = "",
        cfg: str = "",
        sampler: str = "",
        scheduler: str = "",
        model: str = "",
    ) -> tuple[Any, ...]:
        """Stage an album for admin review; return IMAGE unchanged.

        ``frames`` are pre-validated (2-10 items) by :meth:`publish`.
        Runs the SAME pre-flight as the synchronous path (config
        resolution, per-frame duplicate check, encoding, caption
        rendering, hashing), stores the FIRST frame's bytes as the
        review payload, and returns WITHOUT sending media. Review is
        inherently deferred, so ``wait_for_upload=False`` is ignored
        here (info log).
        """
        count = len(frames)
        if not wait_for_upload:
            _logger.info(
                "telegram review mode: wait_for_upload=False is ignored;"
                " publish is deferred until approval"
            )
        _logger.info(
            "telegram album publish review start account=%s destination=%s"
            " frames=%s format=%s quality=%s",
            account,
            destination,
            count,
            format,
            quality,
        )
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
                # First hit refuses the WHOLE album before staging.
                # Fail-open on DB trouble; only DuplicateError refuses.
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
            job_id = _store_review(
                self._config_store,
                self._secret_store,
                self._client_factory,
                self._db_path,
                dest=dest,
                acct=acct,
                first_frame_bytes=bytes(first.data),
                filename=filename,
                image_hash=hashes[0],
                caption_text=caption_text,
                metadata=metadata,
                admin_ids=admin_ids,
            )
        except Exception as exc:
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
                            attempts=0,
                            error_code=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
            except Exception as history_exc:
                _logger.warning("telegram history write failed: %s", history_exc)
            message = f"Telegram publish failed: {friendly_message(exc, action='publish')}"
            _logger.error(
                "telegram album publish review failed account=%s destination=%s"
                " error=%s",
                account,
                destination,
                message,
            )
            raise RuntimeError(message) from exc
        _logger.info(
            "telegram album publish pending review job_id=%s account=%s"
            " destination=%s frames=%s",
            job_id,
            account,
            destination,
            count,
        )
        return (images,)

    def _publish_background(
        self,
        frames: list[Any],
        images: Any,
        account: str,
        destination: str,
        caption: str = "",
        format: str = "png",
        quality: int = 90,
        protect_content: bool = False,
        disable_notification: bool = False,
        skip_duplicate: bool = False,
        prompt: str = "",
        negative_prompt: str = "",
        seed: str = "",
        steps: str = "",
        cfg: str = "",
        sampler: str = "",
        scheduler: str = "",
        model: str = "",
    ) -> tuple[Any, ...]:
        """Enqueue IMAGE frames as an album; return IMAGE immediately.

        ``frames`` are pre-validated (2-10 items) by :meth:`publish`.
        Runs the SAME pre-flight as the synchronous path (config
        resolution, per-frame duplicate check, encoding, caption
        rendering, hashing) and then enqueues an ``album`` payload -- no
        network on this call. Duplicate, caption, and enqueue failures
        raise synchronously (fail fast BEFORE enqueue, wrapped as
        ``RuntimeError`` like the sync path); the token is never logged.
        """
        count = len(frames)
        _logger.info(
            "telegram album publish background start account=%s destination=%s"
            " frames=%s format=%s quality=%s",
            account,
            destination,
            count,
            format,
            quality,
        )
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
            # Fail-fast only: resolved and discarded. The worker's sender
            # re-resolves the token fresh per job (rotation-safe).
            _ = self._config_store.resolve_token(acct.id, self._secret_store)
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
                # First hit refuses the WHOLE album before enqueue.
                # Fail-open on DB trouble; only DuplicateError refuses.
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
            template_vars = metadata.as_template_vars()
            template_vars["image_hash"] = hashes[0]
            template_vars["filename"] = filename
            payload = PublishPayload(
                kind="album",
                account_id=acct.id,
                destination_id=dest.id,
                chat_id=dest.chat_id,
                files=tuple(
                    (bytes(item.data), filename) for item in encoded_frames
                ),
                caption=caption_text,
                protect_content=bool(protect_content),
                disable_notification=bool(disable_notification),
                metadata=template_vars,
            )
            job_id = self._queue_factory().enqueue(payload)
        except Exception as exc:
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
                            attempts=0,
                            error_code=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
            except Exception as history_exc:
                _logger.warning("telegram history write failed: %s", history_exc)
            message = f"Telegram publish failed: {friendly_message(exc, action='publish')}"
            _logger.error(
                "telegram album publish background failed account=%s"
                " destination=%s error=%s",
                account,
                destination,
                message,
            )
            raise RuntimeError(message) from exc
        _logger.info(
            "telegram album publish queued job_id=%s account=%s destination=%s"
            " frames=%s",
            job_id,
            account,
            destination,
            count,
        )
        return (images,)


register(TelegramSendAlbum, "Telegram Send Album", "Telegram Send Album")
