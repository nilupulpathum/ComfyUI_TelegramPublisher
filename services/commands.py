"""Telegram command receiver: T060 framework + T061 /status + T062 /queue
+ T064 approve/reject + T065 run.

Read-only remote inspection plus gated remote control. Mutating commands
(``approve``/``reject``/``run``) are registered via :func:`register_review`
and additionally verify the admin allowlist per call (defense in depth:
the receiver already ignores non-allowlisted chats upstream, and the
handlers return ``"not authorized"`` for direct calls from other chats).

Security contract (non-negotiable):

- Allowlist fail-closed: commands from chats outside the explicit admin
  allowlist are ignored with a warning log (chat_id only) and NO reply,
  so scanners get no oracle. An empty/missing allowlist ignores everything.
- Disabled by default: this module creates no threads and polls nothing
  at import. The host explicitly constructs :class:`CommandReceiver` and
  drives :meth:`CommandReceiver.poll_once`; any looping lives with the host.
- Replies carry status/counts/ids plus at most the first 80 chars of a
  caption. Captions are the admin's own text shown back only on the
  admin-allowlisted channel, and the 80-char truncation keeps listings
  useful for review triage without dumping full prompt text. As a
  backstop, token-shaped substrings are redacted from handler replies.
- The bot token travels only in API URLs (as in :mod:`telegram.client`);
  nothing new is logged: logs carry chat ids only, never message text,
  captions, URLs, or tokens. Error replies are fixed strings, never
  tracebacks or token content.

Stdlib + existing layers only; no ComfyUI imports; no network here
(the injected client owns all HTTP/token handling).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from services.review import ReviewStore
from storage.config import (
    LOOPBACK_HOSTS,
    BotSettings,
    ConfigStore,
    SecretStore,
)
from storage.repositories import GenerationRepository, PublishJobRepository
from telegram.errors import redact
from telegram.logging import get_logger
from telegram.models import Update, parse_update

if TYPE_CHECKING:
    from services.queue import PublishQueue
    from telegram.client import TelegramClient

#: Max caption chars included in a /queue listing line (see module docstring).
CAPTION_PREVIEW_CHARS = 80

#: Fixed reply for handler failures (never a traceback, never a token).
ERROR_REPLY = "error processing command"

_logger = get_logger("services.commands")


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """Parse ``text`` into ``(command, args)``; None when not a command.

    ``text`` must start with ``"/"`` (after surrounding whitespace is
    stripped). The leading slash is removed, the remainder is split on
    whitespace, an ``"@botname"`` suffix is stripped from the command token
    (``"/status@MyBot"`` -> ``("status", [])``), and the command name is
    lowercased. Empty commands (``"/"``, ``"/  "``) yield None. Never
    raises: weird input yields None.
    """
    try:
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        body = stripped[1:].strip()
        if not body:
            return None
        parts = body.split()
        if not parts:
            return None
        name = parts[0].split("@", 1)[0].strip().lower()
        if not name:
            return None
        return name, list(parts[1:])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Context / handler protocol
# ---------------------------------------------------------------------------


@dataclass
class IncomingCommand:
    """One parsed command addressed to the bot from an allowlisted chat."""

    name: str
    args: list[str]
    chat_id: str
    from_id: str | None
    update_id: int


@dataclass
class BotContext:
    """Everything command handlers need (follow-up agent reuses as-is)."""

    client: TelegramClient | Any
    config: ConfigStore
    secrets: SecretStore
    jobs: PublishJobRepository
    generations: GenerationRepository | None
    queue: PublishQueue | Any | None
    settings: BotSettings
    review_dir: Path


CommandHandler = Callable[[BotContext, IncomingCommand], str]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class CommandRouter:
    """Name -> handler registry with quiet-unknown and safe-error semantics."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._handlers: OrderedDict[str, CommandHandler] = OrderedDict()
        self._log = logger if logger is not None else _logger

    def register(self, name: str, handler: CommandHandler) -> None:
        """Register ``handler`` under ``name`` (case-insensitive).

        Raises:
            ValueError: If ``name`` is not a non-empty string, ``handler``
                is not callable, or ``name`` is already registered.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("command name must be a non-empty string")
        if not callable(handler):
            raise ValueError(f"handler for '{name}' must be callable")
        key = name.strip().lower()
        if key in self._handlers:
            raise ValueError(f"command '{key}' is already registered")
        self._handlers[key] = handler

    def names(self) -> list[str]:
        """Registered command names in registration order."""
        return list(self._handlers.keys())

    def handle(self, ctx: BotContext, cmd: IncomingCommand) -> str | None:
        """Run the handler for ``cmd``; None means "send no reply".

        Unknown commands return None (bots stay quiet on unknown text).
        Handler exceptions and non-string results become the fixed
        :data:`ERROR_REPLY` string (logged, never a traceback/token).
        """
        try:
            key = cmd.name.strip().lower() if isinstance(cmd.name, str) else ""
        except Exception:
            return None
        handler = self._handlers.get(key)
        if handler is None:
            return None
        try:
            result = handler(ctx, cmd)
        except Exception as exc:
            try:
                self._log.warning(
                    "command failed name=%s chat_id=%s error=%s",
                    key,
                    getattr(cmd, "chat_id", "?"),
                    type(exc).__name__,
                )
            except Exception:
                pass
            return ERROR_REPLY
        if result is None:
            return None
        if not isinstance(result, str):
            try:
                self._log.warning(
                    "command returned non-string name=%s chat_id=%s",
                    key,
                    getattr(cmd, "chat_id", "?"),
                )
            except Exception:
                pass
            return ERROR_REPLY
        return result


# ---------------------------------------------------------------------------
# Built-in read-only handlers (T061 /status, T062 /queue, help)
# ---------------------------------------------------------------------------


def _counts(jobs: PublishJobRepository) -> dict[str, int]:
    try:
        raw = jobs.count_by_status()
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _status_text(ctx: BotContext) -> str:
    counts = _counts(ctx.jobs)
    queued = int(counts.get("queued", 0))
    sending = int(counts.get("sending", 0))
    success = int(counts.get("success", 0))
    failed = int(counts.get("failed", 0))
    if ctx.queue is None:
        worker = "off"
        buffered = "-"
        bound = "-"
    else:
        try:
            snapshot = ctx.queue.status()
        except Exception:
            worker = "unknown"
            buffered = "-"
            bound = "-"
        else:
            worker = (
                "running" if getattr(snapshot, "worker_running", False) else "stopped"
            )
            buffered = str(getattr(snapshot, "buffered", "-"))
            bound = str(getattr(snapshot, "maxsize", "-"))
    try:
        recent = ctx.jobs.list_recent(1)
    except Exception:
        recent_line = "recent: unknown"
    else:
        if not recent:
            recent_line = "recent: none"
        else:
            job = recent[0]
            recent_line = f"recent: {job.id} {job.destination_id} {job.status}"
    return redact(
        f"queue: queued={queued} sending={sending} "
        f"success={success} failed={failed} worker={worker} "
        f"buffered={buffered}/{bound}\n{recent_line}"
    )


def _preview(caption: str | None) -> str:
    if not isinstance(caption, str) or not caption:
        return "(no caption)"
    return caption[:CAPTION_PREVIEW_CHARS]


def _queue_text(ctx: BotContext) -> str:
    try:
        jobs = ctx.jobs.list_recent(50)
    except Exception:
        return ERROR_REPLY
    active = [j for j in jobs if getattr(j, "status", None) in ("queued", "sending")]
    if not active:
        return "queue is empty"
    n_queued = sum(1 for j in active if j.status == "queued")
    n_sending = sum(1 for j in active if j.status == "sending")
    lines = [f"queued: {n_queued} sending: {n_sending}"]
    for job in active:
        lines.append(
            f"{job.id} {job.destination_id} {job.status} "
            f"attempts={job.attempts} {_preview(job.caption)}"
        )
    # Backstop: captions are admin-authored free text; mask anything
    # token-shaped before it reaches a reply.
    return redact("\n".join(lines))


_BUILTIN_DESCRIPTIONS = {
    "help": "list commands",
    "status": "show queue status",
    "queue": "list queued jobs",
}


def register_builtins(router: CommandRouter) -> CommandRouter:
    """Register the read-only ``help``/``status``/``queue`` handlers."""

    def _help(ctx: BotContext, cmd: IncomingCommand) -> str:
        del ctx, cmd
        lines = ["commands:"]
        for name in sorted(router.names()):
            desc = _BUILTIN_DESCRIPTIONS.get(name, "")
            lines.append(f"/{name} - {desc}" if desc else f"/{name}")
        return "\n".join(lines)

    def _status(ctx: BotContext, cmd: IncomingCommand) -> str:
        del cmd
        return _status_text(ctx)

    def _queue(ctx: BotContext, cmd: IncomingCommand) -> str:
        del cmd
        return _queue_text(ctx)

    router.register("help", _help)
    router.register("status", _status)
    router.register("queue", _queue)
    return router


# ---------------------------------------------------------------------------
# Mutating handlers: T064 approve/reject + T065 run (via register_review)
# ---------------------------------------------------------------------------

#: Job ids are validated before any filesystem use (path traversal guard).
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: Max caption chars quoted in a review notification (matches /queue preview).
REVIEW_NOTIFY_PREVIEW_CHARS = 80

#: Max prompt-file size a /run trigger will read (5 MB; larger is refused).
PROMPT_FILE_MAX_BYTES = 5 * 1024 * 1024

#: HTTP timeout (seconds) for the trigger POST to the local ComfyUI server.
TRIGGER_HTTP_TIMEOUT = 30.0


def _valid_job_id(value: object) -> bool:
    return isinstance(value, str) and _JOB_ID_RE.fullmatch(value) is not None


def _is_admin(ctx: BotContext, cmd: IncomingCommand) -> bool:
    """Defense-in-depth allowlist check for mutating commands.

    The receiver already ignores non-allowlisted chats upstream; handlers
    re-verify against ``ctx.settings.admin_chat_ids`` so direct handler
    calls from other chats return ``"not authorized"`` instead of acting.
    Fail-closed: an unreadable/empty allowlist authorizes nobody.
    """
    try:
        admins = {str(c) for c in ctx.settings.admin_chat_ids}
    except Exception:
        return False
    try:
        return str(cmd.chat_id) in admins
    except Exception:
        return False


def _approve(ctx: BotContext, cmd: IncomingCommand) -> str:
    if not _is_admin(ctx, cmd):
        return "not authorized"
    arg = cmd.args[0] if cmd.args else ""
    if not _valid_job_id(arg):
        return f"unknown review job '{arg}'"
    job = ctx.jobs.get(arg)
    if job is None:
        return f"unknown review job '{arg}'"
    if job.status != "pending_review":
        return f"already handled (status {job.status})"
    store = ReviewStore(ctx.review_dir)
    try:
        payload = store.load(arg)
    except (ValueError, OSError):
        job.status = "failed"
        job.error_code = "PayloadLost"
        job.error_message = "review payload missing"
        ctx.jobs.update(job)
        return "review payload missing; job marked failed"
    try:
        dest = ctx.config.get_destination(job.destination_id)
        acct = ctx.config.get_account(dest.account_id)
        # Validated only: the send goes through ctx.client (the polled bot).
        # A missing account/token is a configuration error, never a send.
        ctx.config.resolve_token(acct.id, ctx.secrets)
    except Exception:
        return "configuration error: cannot resolve destination or credentials"
    # Unexpected send failures RAISE (receiver maps them to the fixed
    # ERROR_REPLY; detail stays in logs, never in the reply).
    result = ctx.client.send_photo(
        dest.chat_id,
        payload,
        job.filename or f"review_{arg}.png",
        job.caption or None,
    )
    message_id = getattr(result, "message_id", None)
    job.status = "success"
    job.telegram_message_id = str(message_id)
    try:
        job.attempts = int(job.attempts) + 1
    except (TypeError, ValueError):
        job.attempts = 1
    job.error_code = None
    job.error_message = None
    ctx.jobs.update(job)
    try:
        store.discard(arg)
    except (ValueError, OSError):
        _logger.warning("review discard failed job_id=%s", arg)
    return redact(f"published to {dest.name}, message {message_id}")


def _reject(ctx: BotContext, cmd: IncomingCommand) -> str:
    if not _is_admin(ctx, cmd):
        return "not authorized"
    arg = cmd.args[0] if cmd.args else ""
    if not _valid_job_id(arg):
        return f"unknown review job '{arg}'"
    job = ctx.jobs.get(arg)
    if job is None:
        return f"unknown review job '{arg}'"
    if job.status != "pending_review":
        return f"already handled (status {job.status})"
    try:
        dest = ctx.config.get_destination(job.destination_id)
        dest_name = dest.name
    except Exception:
        return "configuration error: cannot resolve destination or credentials"
    job.status = "failed"
    job.error_code = "Rejected"
    job.error_message = "rejected by admin"
    ctx.jobs.update(job)
    try:
        ReviewStore(ctx.review_dir).discard(arg)
    except (ValueError, OSError):
        _logger.warning("review discard failed job_id=%s", arg)
    return redact(f"rejected publish to {dest_name}")


def _run(ctx: BotContext, cmd: IncomingCommand) -> str:
    if not _is_admin(ctx, cmd):
        return "not authorized"
    if not cmd.args:
        return "usage: /run <name>"
    name = cmd.args[0]
    try:
        triggers = tuple(ctx.settings.triggers)
    except Exception:
        triggers = ()
    match = next((t for t in triggers if getattr(t, "name", None) == name), None)
    if match is None:
        if not triggers:
            return f"unknown trigger '{name}' (no triggers configured)"
        return f"unknown trigger '{name}'"
    path = Path(match.prompt_file)
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        return "trigger misconfigured: prompt file missing"
    try:
        size = path.stat().st_size
    except OSError:
        return "trigger misconfigured: prompt file missing"
    if size > PROMPT_FILE_MAX_BYTES:
        return "trigger misconfigured: prompt file too large"
    try:
        raw = path.read_bytes()
    except OSError:
        return "trigger misconfigured: prompt file missing"
    if len(raw) > PROMPT_FILE_MAX_BYTES:
        return "trigger misconfigured: prompt file too large"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "trigger failed: prompt file is not valid JSON"
    host = getattr(ctx.settings, "comfy_host", None)
    port = getattr(ctx.settings, "comfy_port", None)
    # Defense in depth: re-validated here, beyond save-time validation, so
    # a hand-edited settings object can never aim the trigger at a remote
    # host.
    if host not in LOOPBACK_HOSTS:
        return "trigger misconfigured: comfy_host must be loopback"
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        return "trigger misconfigured: comfy_port must be an int in 1..65535"
    url = f"http://{host}:{port}/prompt"
    body = json.dumps({"prompt": parsed}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=TRIGGER_HTTP_TIMEOUT
        ) as resp:
            resp_raw = resp.read()
    except urllib.error.HTTPError as exc:
        return redact(f"trigger failed: ComfyUI error HTTP {exc.code}")
    except Exception as exc:
        return redact(
            f"trigger failed: cannot reach ComfyUI ({type(exc).__name__})"
        )
    try:
        resp_json = json.loads(resp_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "trigger failed: ComfyUI returned malformed data"
    prompt_id = (
        resp_json.get("prompt_id") if isinstance(resp_json, dict) else None
    )
    if not isinstance(prompt_id, str) or not prompt_id:
        return "trigger failed: ComfyUI returned malformed data"
    return redact(f"workflow started: {prompt_id}")


def register_review(router: CommandRouter) -> CommandRouter:
    """Register the mutating ``approve``/``reject``/``run`` handlers.

    Built-in read-only handlers from :func:`register_builtins` are left
    untouched; names collide loudly via :meth:`CommandRouter.register`.
    """
    router.register("approve", _approve)
    router.register("reject", _reject)
    router.register("run", _run)
    return router


# ---------------------------------------------------------------------------
# Offset persistence
# ---------------------------------------------------------------------------


class OffsetStore:
    """File-backed last ``getUpdates`` offset (JSON ``{"offset": int}``)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> int | None:
        """Return the stored offset; None when the file is missing.

        Raises:
            ValueError: If the file exists but is corrupt (bad JSON,
                non-object shape, or a non-int ``offset``). Loud by design:
                silently restarting from None could reprocess history.
        """
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise ValueError(
                f"offset file '{self._path}' is corrupt: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(f"offset file '{self._path}' must hold a JSON object")
        if "offset" not in raw:
            raise ValueError(f"offset file '{self._path}' has no 'offset' key")
        offset = raw["offset"]
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError(
                f"offset file '{self._path}' has an invalid offset"
            )
        return offset

    def set(self, offset: int) -> None:
        """Persist ``offset``.

        Raises:
            ValueError: If ``offset`` is not a non-negative int.
        """
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative int")
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"offset": offset}, indent=2, sort_keys=True),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Receiver (T060; host-driven, no threads, no autostart)
# ---------------------------------------------------------------------------


class CommandReceiver:
    """Poll ``getUpdates`` once per :meth:`poll_once` call and reply.

    The receiver never acts on chats outside ``allowed_chat_ids``: such
    updates are skipped with a warning log (chat_id only) and no reply.
    An empty allowlist is fail-closed (everything ignored, stays silent).
    Client errors propagate loudly to the host; per-reply send failures
    are isolated (warning + continue).
    """

    def __init__(
        self,
        client: TelegramClient | Any,
        router: CommandRouter,
        allowed_chat_ids: Any,
        offset_store: OffsetStore | Any,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        if not callable(getattr(client, "get_updates", None)):
            raise ValueError("client must expose get_updates()")
        if not callable(getattr(client, "send_message", None)):
            raise ValueError("client must expose send_message()")
        if not callable(getattr(router, "handle", None)):
            raise ValueError("router must expose handle()")
        if offset_store is not None and (
            not callable(getattr(offset_store, "get", None))
            or not callable(getattr(offset_store, "set", None))
        ):
            raise ValueError("offset_store must expose get()/set()")
        self._client = client
        self._router = router
        self._offset_store = offset_store
        self._allowed = _normalize_allowlist(allowed_chat_ids)
        self._log = logger if logger is not None else _logger

    def poll_once(self, ctx: BotContext, *, timeout_s: int | float = 30) -> list[tuple[str, str]]:
        """Fetch, filter, handle, and reply once. Return sent ``(chat, reply)``.

        Offset advances past EVERY processed update id (even skipped
        ones -- never reprocess) and is persisted when it moves forward.
        Client errors raise to the host; one reply's send failure never
        aborts the others.
        """
        current = self._offset_store.get() if self._offset_store is not None else None
        # Client errors (auth/network/validation) propagate loudly to host.
        raw_updates = self._client.get_updates(offset=current, timeout=timeout_s)
        if not isinstance(raw_updates, list):
            raise ValueError("getUpdates returned malformed data")
        sent: list[tuple[str, str]] = []
        max_id: int | None = None
        for raw in raw_updates:
            if isinstance(raw, dict):
                uid = raw.get("update_id")
                if isinstance(uid, int) and not isinstance(uid, bool):
                    max_id = uid if max_id is None else max(max_id, uid)
            try:
                update: Update | None = parse_update(raw)
            except Exception:
                update = None  # backstop; parse_update never raises by contract
            if update is None:
                continue
            if update.chat_id not in self._allowed:
                try:
                    self._log.warning(
                        "ignoring update from non-allowlisted chat chat_id=%s",
                        update.chat_id,
                    )
                except Exception:
                    pass
                continue
            try:
                parsed = parse_command(update.text)
            except Exception:
                parsed = None  # backstop; parse_command never raises by contract
            if parsed is None:
                continue
            cmd = IncomingCommand(
                name=parsed[0],
                args=parsed[1],
                chat_id=update.chat_id,
                from_id=update.from_id,
                update_id=update.update_id,
            )
            try:
                reply = self._router.handle(ctx, cmd)
            except Exception as exc:
                try:
                    self._log.warning(
                        "command handling failed chat_id=%s error=%s",
                        update.chat_id,
                        type(exc).__name__,
                    )
                except Exception:
                    pass
                reply = ERROR_REPLY
            if reply is None:
                continue
            if not isinstance(reply, str) or not reply:
                continue
            try:
                self._client.send_message(update.chat_id, reply)
            except Exception as exc:
                try:
                    self._log.warning(
                        "reply send failed chat_id=%s error=%s",
                        update.chat_id,
                        type(exc).__name__,
                    )
                except Exception:
                    pass
                continue
            sent.append((update.chat_id, reply))
        if max_id is not None and self._offset_store is not None:
            next_offset = max_id + 1
            if current is None or next_offset > current:
                self._offset_store.set(next_offset)
        return sent


def _normalize_allowlist(allowed: Any) -> frozenset:
    """Normalize the admin allowlist to a frozenset of chat-id strings."""
    if allowed is None:
        return frozenset()
    if isinstance(allowed, (str, bytes)):
        return frozenset({str(allowed)})
    try:
        items = list(allowed)
    except TypeError:
        raise ValueError(
            "allowed_chat_ids must be an iterable of chat id strings"
        ) from None
    return frozenset(str(c) for c in items)
