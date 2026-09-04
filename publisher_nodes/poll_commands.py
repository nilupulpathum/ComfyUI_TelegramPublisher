"""ComfyUI adapter: Telegram Command Poller node (T070, Epic 7 driver).

Node-layer only: drives the Epic 6 command receiver
(:class:`services.commands.CommandReceiver`) while its prompt runs, so bot
commands actually respond in a live install. No torch/ComfyUI imports here
and no raw HTTP (all network goes through the injected client factory).

Decisions:

- Explicit driver, no autostart: this module creates no threads, starts no
  workers, and polls nothing at import. ComfyUI executes :meth:`poll`
  synchronously; worst-case blocking is ``poll_rounds x poll_timeout``
  seconds (default 3 x 10s = 30s). Each ``getUpdates`` long-poll also
  bounds its own HTTP read timeout above the poll window (see
  :meth:`telegram.client.TelegramClient.get_updates`).
- Fail-closed degrade: when ``settings.admin_chat_ids`` is empty, the node
  logs a warning and returns a skip summary WITHOUT creating a client or
  making any network call (scanners get no oracle, same stance as the
  receiver). An empty poll (zero new commands) is not an error: it
  returns ``"no new commands"`` normally.
- ``queue=None`` in the bot context on purpose: ``/status`` then reports
  ``worker=off``. Starting the process-wide shared worker here would be a
  side effect beyond polling (use a Send node with ``wait_for_upload=False``
  when a background worker is wanted); see :func:`_note_queue_off`.
- Offset files live at ``<db parent>/offsets/<safe-account-id>.json``
  where the account id is sanitized to ``[A-Za-z0-9_-]`` (other chars map
  to ``"_"``). Collisions from sanitization are harmless: approve/reject
  are idempotent and an offset rewind only reprocesses updates (the
  receiver skips already-handled work and advances the offset again).
- History (SQLite ``publish_jobs`` + ``generations``) is lazy and
  best-effort like the siblings: ``init_schema()`` runs inside try/except
  and any failure warns without blocking polling (handlers degrade to
  empty counts / the fixed error reply).
- Review GC host: every run with admins configured calls
  ``services.review.prune_expired`` once (default TTL) BEFORE the first
  ``getUpdates`` poll, so abandoned ``pending_review`` payloads expire
  without a separate node or thread. GC is best-effort: any failure
  warns and polling continues; counts log at info level.
- Every failure surfaces as ``RuntimeError`` carrying an actionable,
  token-free message (same contract as the publishers); the original is
  chained. The bot token is never interpolated into messages or logs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from publisher_nodes import register
from publisher_nodes.send_image import (
    _account_options,
    _DEFAULT_CONFIG_PATH,
    _DEFAULT_HISTORY_PATH,
    _DEFAULT_SECRET_PATH,
)
from services.commands import (
    BotContext,
    CommandReceiver,
    CommandRouter,
    OffsetStore,
    register_builtins,
    register_review,
)
from services.review import prune_expired, review_dir_for
from storage.config import ConfigStore, FileSecretStore
from storage.database import Database
from storage.repositories import GenerationRepository, PublishJobRepository
from telegram.client import TelegramClient
from telegram.friendly import friendly_message
from telegram.logging import get_logger

_logger = get_logger(__name__)

#: Skip summary returned (with zero network calls) when no admin chats
#: are configured. Tests assert on the ``"no admin chats configured"``
#: prefix; keep it stable.
SKIP_NO_ADMINS = (
    "Telegram polling skipped: no admin chats configured; "
    "set settings.admin_chat_ids to enable bot commands."
)

#: Anything outside this class maps to "_" in offset file names.
_SAFE_ACCOUNT_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_account_id(account_id: str) -> str:
    """Map ``account_id`` to a filesystem-safe offset file stem.

    Every char outside ``[A-Za-z0-9_-]`` becomes ``"_"``. An empty result
    maps to ``"_"`` so the caller always gets a non-empty stem.
    Collisions are harmless (see module docstring: approve/reject are
    idempotent and an offset rewind only reprocesses).
    """
    cleaned = _SAFE_ACCOUNT_RE.sub("_", account_id or "")
    return cleaned or "_"


def default_offset_path(db_path: str | Path, account_id: str) -> Path:
    """Return ``<db parent>/offsets/<safe-account-id>.json`` for ``db_path``."""
    return Path(db_path).parent / "offsets" / f"{sanitize_account_id(account_id)}.json"


def _note_queue_off() -> None:
    """Document the ``queue=None`` choice (called for its docstring)."""


_note_queue_off.__doc__ = (
    "The poller passes ``queue=None`` into BotContext on purpose: /status "
    "then reports worker=off, and no background worker is started as a "
    "side effect of polling. Do NOT call services.queue.get_shared_queue "
    "here."
)


class TelegramCommandPoller:
    """Long-poll ``getUpdates`` and dispatch bot commands; return a summary."""

    RETURN_TYPES = ("STRING",)
    FUNCTION = "poll"
    CATEGORY = "Telegram"
    OUTPUT_NODE = True

    def __init__(
        self,
        config_store: ConfigStore | None = None,
        secret_store: FileSecretStore | None = None,
        client_factory: Callable[[str], Any] | None = None,
        db_path: str | Path | None = None,
        offset_path: str | Path | None = None,
    ) -> None:
        # Injectable seams mirroring the siblings: tests pass tmp-path
        # stores, a fake client factory, a tmp db_path, and a tmp
        # offset_path so no ComfyUI, network, real secrets, or repo files
        # are ever needed. ``offset_path=None`` resolves per account at
        # poll time via default_offset_path (the account id is only known
        # then).
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
        self._offset_path = Path(offset_path) if offset_path is not None else None

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "account": _account_options(),
                "poll_rounds": ("INT", {"default": 3, "min": 1, "max": 20}),
                "poll_timeout": ("INT", {"default": 10, "min": 1, "max": 30}),
            },
        }

    def poll(
        self,
        account: str,
        poll_rounds: int = 3,
        poll_timeout: int = 10,
    ) -> tuple[str, ...]:
        """Poll bot commands ``poll_rounds`` times and return ``(summary,)``.

        Worst-case blocking is ``poll_rounds x poll_timeout`` seconds
        (default 3 x 10s = 30s): each round is one ``getUpdates``
        long-poll of ``poll_timeout`` seconds. No threads are created and
        nothing autostarts; an empty poll returns ``"no new commands"``
        (not an error). The summary is always a one-element tuple of
        ``str`` (``"<n> replies sent"``, ``"no new commands"``, or the
        skip message) -- never None.
        """
        rounds = _require_rounds(poll_rounds)
        timeout = _require_timeout(poll_timeout)
        try:
            acct = self._config_store.get_account(account)
            # Local only; never stored on the node, logged, or put in errors.
            token = self._config_store.resolve_token(acct.id, self._secret_store)
        except Exception as exc:
            message = f"Telegram polling failed: {friendly_message(exc, action='poll')}"
            _logger.error(
                "telegram polling failed account=%s error=%s",
                account,
                message,
            )
            raise RuntimeError(message) from exc
        try:
            settings = self._config_store.get_settings()
            admin_ids = tuple(settings.admin_chat_ids)
        except Exception as exc:
            message = f"Telegram polling failed: {friendly_message(exc, action='poll')}"
            _logger.error(
                "telegram polling failed account=%s error=%s",
                account,
                message,
            )
            raise RuntimeError(message) from exc
        if not admin_ids:
            # Fail-closed degrade with ZERO network calls: no client is
            # created and no getUpdates is issued below this line.
            _logger.warning(
                "telegram polling skipped account=%s: no admin chats configured",
                account,
            )
            return (SKIP_NO_ADMINS,)
        try:
            return self._poll_with_admins(acct.id, token, admin_ids, rounds, timeout)
        except Exception as exc:
            message = f"Telegram polling failed: {friendly_message(exc, action='poll')}"
            _logger.error(
                "telegram polling failed account=%s error=%s",
                account,
                message,
            )
            raise RuntimeError(message) from exc

    def _poll_with_admins(
        self,
        account_id: str,
        token: str,
        admin_ids: tuple[str, ...],
        rounds: int,
        timeout: int,
    ) -> tuple[str, ...]:
        """Build the Epic 6 stack, expire stale reviews, run ``rounds`` polls.

        Review expiry (``services.review.prune_expired`` with the default
        TTL) runs once here, before the first ``getUpdates`` poll, making
        this driver the GC host (T071). It is best-effort: any failure
        warns and polling continues normally.
        """
        try:
            db = Database(self._db_path)
            db.init_schema()
        except Exception as exc:
            # Best-effort like the siblings: handlers degrade without
            # history (empty counts / fixed error reply) rather than
            # blocking command polling.
            _logger.warning("telegram history unavailable: %s", exc)
            db = Database(self._db_path)
        jobs = PublishJobRepository(db)
        generations = GenerationRepository(db)
        review_dir = review_dir_for(self._db_path)
        try:
            expiry = prune_expired(review_dir, jobs)
        except Exception as exc:
            # Best-effort GC: expiry must never fail a polling run.
            _logger.warning("telegram review expiry failed: %s", exc)
        else:
            _logger.info(
                "telegram review expiry account=%s expired=%s orphans=%s kept=%s",
                account_id,
                expiry.get("expired", 0),
                expiry.get("orphans", 0),
                expiry.get("kept", 0),
            )
        client = self._client_factory(token)
        ctx = BotContext(
            client=client,
            config=self._config_store,
            secrets=self._secret_store,
            jobs=jobs,
            generations=generations,
            # Deliberately None: starting the shared worker here would be
            # a side effect beyond polling; /status shows worker=off.
            queue=None,
            settings=self._config_store.get_settings(),
            review_dir=review_dir,
        )
        router: CommandRouter = register_review(register_builtins(CommandRouter()))
        offset_path = (
            self._offset_path
            if self._offset_path is not None
            else default_offset_path(self._db_path, account_id)
        )
        receiver = CommandReceiver(client, router, admin_ids, OffsetStore(offset_path))
        total = 0
        for _ in range(rounds):
            total += len(receiver.poll_once(ctx, timeout_s=timeout))
        _logger.info(
            "telegram polling done account=%s rounds=%s replies=%s",
            account_id,
            rounds,
            total,
        )
        if total:
            return (f"{total} replies sent",)
        return ("no new commands",)


def _require_rounds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("poll_rounds must be an int in 1..20")
    if not 1 <= value <= 20:
        raise ValueError("poll_rounds must be an int in 1..20")
    return value


def _require_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("poll_timeout must be an int in 1..30 seconds")
    if not 1 <= value <= 30:
        raise ValueError("poll_timeout must be an int in 1..30 seconds")
    return value


register(TelegramCommandPoller, "Telegram Command Poller", "Telegram Command Poller")
