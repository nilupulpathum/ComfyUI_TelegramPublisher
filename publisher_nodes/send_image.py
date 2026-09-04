"""ComfyUI adapter: Telegram Send Image node (T009 + Epic 3 integration).

Node-layer only: adapts ComfyUI inputs/outputs to application services.
No torch/ComfyUI imports here (passthrough needs no tensor ops) and no
raw HTTP (all network goes through :class:`TelegramClient`).

Decisions:

- ``account`` / ``destination`` are plain STRING ids. Rich selector
  widgets (account/destination dropdowns) are later-epic work (T051/T052);
  until then users paste the ids from their local config.
- ``wait_for_upload=True`` (default) publishes synchronously as before.
  ``wait_for_upload=False`` runs the SAME pre-flight (config resolution,
  duplicate check, encoding, caption rendering, hashing) and then
  enqueues a :class:`services.queue.PublishPayload` on the process-wide
  shared queue, returning the input IMAGE immediately without network.
  The background worker uploads later and moves the persisted
  ``publish_jobs`` row to ``success``/``failed``. Validation, duplicate,
  and caption errors still raise synchronously on the background path
  (fail fast before enqueue).
- ``caption`` is a template rendered with generation metadata
  (``services.captions.render_caption``). Unknown placeholders resolve to
  ``""`` with a logged warning (FR-009 fail-safe); the send proceeds.
- ``skip_duplicate=True`` performs an exact-payload SHA-256 check scoped
  to the destination via :class:`services.dedup.DuplicateDetector` (live
  since T034). A hit refuses the publish loudly. A duplicate-lookup DB
  failure is fail-open: logged as a warning, publish proceeds.
- History (SQLite ``publish_jobs`` + ``generations``) is lazy and
  best-effort: the database is opened and ``init_schema()`` runs on first
  use inside try/except; any history failure logs a warning and the
  publish continues (history NEVER blocks publishing).
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
from services.captions import render_caption
from services.dedup import DuplicateDetector, sha256_hex
from services.encoder import encode_image
from services.metadata import GenerationMetadata
from services.queue import PublishPayload, PublishQueue, get_shared_queue
from storage.config import ConfigStore, FileSecretStore
from storage.database import Database
from storage.repositories import (
    Generation,
    GenerationRepository,
    PublishJob,
    PublishJobRepository,
)
from telegram.client import TelegramClient
from telegram.errors import ConfigurationError, DuplicateError
from telegram.logging import get_logger

# Extension dir derived from inside publisher_nodes/ (approved locations).
_EXTENSION_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_PATH = _EXTENSION_DIR / "user_config" / "accounts.json"
_DEFAULT_SECRET_PATH = _EXTENSION_DIR / "secrets" / "tokens.json"
_DEFAULT_HISTORY_PATH = _EXTENSION_DIR / "history" / "publisher.sqlite3"

_logger = get_logger(__name__)

#: Optional metadata inputs shared by the Send Image and Send Album nodes.
#: ``prompt``/``negative_prompt`` use a multiline widget; the rest are
#: single-line strings. All default to "" (absent metadata renders as "").
OPTIONAL_METADATA_INPUTS: dict[str, Any] = {
    "prompt": ("STRING", {"default": "", "multiline": True}),
    "negative_prompt": ("STRING", {"default": "", "multiline": True}),
    "seed": ("STRING", {"default": "", "multiline": False}),
    "steps": ("STRING", {"default": "", "multiline": False}),
    "cfg": ("STRING", {"default": "", "multiline": False}),
    "sampler": ("STRING", {"default": "", "multiline": False}),
    "scheduler": ("STRING", {"default": "", "multiline": False}),
    "model": ("STRING", {"default": "", "multiline": False}),
}


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


def _history_repos(
    db_path: Path,
) -> tuple[PublishJobRepository, GenerationRepository] | None:
    """Open the history DB and return repos, or None (warn) on any failure.

    Lazy + best-effort: ``init_schema()`` runs here on first use, and any
    failure logs a warning so history NEVER blocks publishing.
    """
    try:
        db = Database(db_path)
        db.init_schema()
        return PublishJobRepository(db), GenerationRepository(db)
    except Exception as exc:
        _logger.warning("telegram history unavailable: %s", exc)
        return None


def _make_sender(
    config_store: ConfigStore, secret_store: FileSecretStore
) -> Callable[[PublishPayload], str]:
    """Build the queue worker's sender: fresh token per job, one attempt.

    The token is resolved FRESH on every call via the stores
    (rotation-safe) and stays a local variable: never stored on the
    node, never logged, never interpolated into error text. The client
    is built with ``retry_policy=None`` (single attempt) on purpose:
    the queue WORKER owns all retries/backoff (see
    :mod:`services.queue`), so a second retry layer here would
    double-delay and risk double-sends (FR-008). Typed telegram errors
    propagate for the worker to map; an unknown payload kind raises
    ``ValueError`` (permanent failure, no retry).
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


def _default_shared_queue(
    config_store: ConfigStore,
    secret_store: FileSecretStore,
    db_path: Path,
) -> PublishQueue:
    """Return the process-wide started queue for this history database.

    Created on first use via :func:`services.queue.get_shared_queue`
    (single attempt sender; the worker owns retries) and shut down by
    its ``atexit`` backstop when the interpreter exits.
    """

    def _factory() -> PublishQueue:
        db = Database(db_path)
        db.init_schema()
        return PublishQueue(db, _make_sender(config_store, secret_store))

    return get_shared_queue(str(db_path), _factory)


def _record_generation(
    gen_repo: GenerationRepository,
    job_id: str,
    metadata: GenerationMetadata,
) -> None:
    """Persist one generation row linked to a publish job."""
    gen_repo.create(
        Generation(
            job_id=job_id,
            prompt=metadata.prompt,
            negative_prompt=metadata.negative_prompt,
            model=metadata.model,
            seed=metadata.seed,
            steps=metadata.steps,
            cfg=metadata.cfg,
            sampler=metadata.sampler,
            scheduler=metadata.scheduler,
            width=metadata.width,
            height=metadata.height,
        )
    )


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
            },
            "optional": dict(OPTIONAL_METADATA_INPUTS),
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
        prompt: str = "",
        negative_prompt: str = "",
        seed: str = "",
        steps: str = "",
        cfg: str = "",
        sampler: str = "",
        scheduler: str = "",
        model: str = "",
    ) -> tuple[Any, ...]:
        """Send the first IMAGE frame to Telegram; return IMAGE unchanged."""
        if not wait_for_upload:
            return self._publish_background(
                image,
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
        _logger.info(
            "telegram publish start account=%s destination=%s format=%s quality=%s",
            account,
            destination,
            format,
            quality,
        )
        start = time.perf_counter()
        # Best-effort history fields: filled as the publish proceeds so a
        # failure can still record a failed job with what is known so far.
        image_hash: str | None = None
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
            frame = _first_frame(image)
            encoded = encode_image(frame, format=format, quality=quality)
            filename = f"comfyui_{encoded.width}x{encoded.height}.{encoded.format}"
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
                    "width": encoded.width,
                    "height": encoded.height,
                    "filename": filename,
                }
            )
            rendered = render_caption(caption, metadata.as_template_vars())
            caption_text = rendered.text
            for warning in rendered.warnings:
                _logger.warning("telegram caption warning: %s", warning)
            image_hash = sha256_hex(encoded.data)
            if skip_duplicate:
                # Fail-open: a broken duplicate lookup warns and proceeds;
                # only a positive DuplicateError refuses the publish.
                repos = _history_repos(self._db_path)
                if repos is not None:
                    job_repo, _ = repos
                    try:
                        DuplicateDetector(job_repo).check(image_hash, dest.id)
                    except DuplicateError:
                        raise
                    except Exception as exc:
                        _logger.warning(
                            "telegram duplicate check failed; proceeding: %s",
                            exc,
                        )
            client = self._client_factory(token)
            result = client.send_photo(
                dest.chat_id,
                encoded.data,
                filename,
                caption_text or None,
                protect_content=bool(protect_content),
                disable_notification=bool(disable_notification),
            )
            try:
                repos = _history_repos(self._db_path)
                if repos is not None:
                    job_repo, gen_repo = repos
                    job = job_repo.create(
                        PublishJob(
                            destination_id=dest.id,
                            status="success",
                            image_hash=image_hash,
                            filename=filename,
                            caption=caption_text,
                            attempts=1,
                            telegram_message_id=str(result.message_id),
                        )
                    )
                    _record_generation(gen_repo, str(job.id), metadata)
            except Exception as exc:
                _logger.warning("telegram history write failed: %s", exc)
        except Exception as exc:
            # Decision (a): every failure surfaces as RuntimeError with the
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
                            image_hash=image_hash,
                            filename=filename,
                            caption=caption_text,
                            attempts=1,
                            error_code=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
            except Exception as history_exc:
                _logger.warning(
                    "telegram history write failed: %s", history_exc
                )
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

    def _publish_background(
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
        prompt: str = "",
        negative_prompt: str = "",
        seed: str = "",
        steps: str = "",
        cfg: str = "",
        sampler: str = "",
        scheduler: str = "",
        model: str = "",
    ) -> tuple[Any, ...]:
        """Enqueue the first IMAGE frame; return IMAGE immediately.

        Runs the SAME pre-flight as the synchronous path (config
        resolution, duplicate check, encoding, caption rendering,
        hashing) and then enqueues a ``photo`` payload -- no network on
        this call. Validation, duplicate, caption, and enqueue failures
        raise synchronously (fail fast BEFORE enqueue, wrapped as
        ``RuntimeError`` like the sync path); the token is never logged.
        """
        _logger.info(
            "telegram publish background start account=%s destination=%s"
            " format=%s quality=%s",
            account,
            destination,
            format,
            quality,
        )
        image_hash: str | None = None
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
            frame = _first_frame(image)
            encoded = encode_image(frame, format=format, quality=quality)
            filename = f"comfyui_{encoded.width}x{encoded.height}.{encoded.format}"
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
                    "width": encoded.width,
                    "height": encoded.height,
                    "filename": filename,
                }
            )
            rendered = render_caption(caption, metadata.as_template_vars())
            caption_text = rendered.text
            for warning in rendered.warnings:
                _logger.warning("telegram caption warning: %s", warning)
            image_hash = sha256_hex(encoded.data)
            if skip_duplicate:
                # Fail-open like the sync path; only a positive
                # DuplicateError refuses the publish (before enqueue).
                repos = _history_repos(self._db_path)
                if repos is not None:
                    job_repo, _ = repos
                    try:
                        DuplicateDetector(job_repo).check(image_hash, dest.id)
                    except DuplicateError:
                        raise
                    except Exception as exc:
                        _logger.warning(
                            "telegram duplicate check failed; proceeding: %s",
                            exc,
                        )
            template_vars = metadata.as_template_vars()
            template_vars["image_hash"] = image_hash
            template_vars["filename"] = filename
            payload = PublishPayload(
                kind="photo",
                account_id=acct.id,
                destination_id=dest.id,
                chat_id=dest.chat_id,
                files=((bytes(encoded.data), filename),),
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
                            image_hash=image_hash,
                            filename=filename,
                            caption=caption_text,
                            attempts=0,
                            error_code=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
            except Exception as history_exc:
                _logger.warning(
                    "telegram history write failed: %s", history_exc
                )
            message = f"Telegram publish failed: {exc}"
            _logger.error(
                "telegram publish background failed account=%s destination=%s"
                " error=%s",
                account,
                destination,
                message,
            )
            raise RuntimeError(message) from exc
        _logger.info(
            "telegram publish queued job_id=%s account=%s destination=%s",
            job_id,
            account,
            destination,
        )
        return (image,)


register(TelegramSendImage, "Telegram Send Image", "Telegram Send Image")
