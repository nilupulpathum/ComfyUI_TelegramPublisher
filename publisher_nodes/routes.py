"""HTTP route adapters for the Telegram Publisher settings/status UI (T053/T054).

Adapter layer only: every handler here is a thin aiohttp wrapper around a
sync, dependency-explicit logic function (``api_*``) that returns a plain
JSON-safe dict. All business logic lives in :mod:`publisher_nodes.web_api`
(peer-owned); this module only adapts it to HTTP and wraps failures in a
token-free ``{"ok": False, "error": ...}`` envelope.

Testability: ``aiohttp`` is imported LAZILY inside :func:`register_routes`
(it exists only inside ComfyUI). The ``api_*`` functions and
:func:`register_routes` (with a fake routes object) are fully testable
without aiohttp and without network. ``web_api`` is consumed via the
contract in the task brief; when the peer module is absent (e.g. agents
working in parallel) small local fallbacks with identical semantics are
used so this module still imports and behaves correctly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from storage.config import ConfigStore, FileSecretStore
from storage.database import Database
from storage.repositories import PublishJobRepository
from telegram.client import TelegramClient
from telegram.errors import redact

try:  # Peer-owned contract module; may land after this file.
    from publisher_nodes import web_api as _web_api
except ImportError:  # Parallel-agent fallback; local shims below match it.
    _web_api = None  # type: ignore[assignment]

# Extension dir derived from inside publisher_nodes/ (approved locations).
_EXTENSION_DIR = Path(__file__).resolve().parents[1]
_FALLBACK_CONFIG_PATH = _EXTENSION_DIR / "user_config" / "accounts.json"
_FALLBACK_SECRET_PATH = _EXTENSION_DIR / "secrets" / "tokens.json"
_FALLBACK_DB_PATH = _EXTENSION_DIR / "history" / "publisher.sqlite3"


def _default_config_path() -> Path:
    if _web_api is not None:
        return Path(_web_api.DEFAULT_CONFIG_PATH)
    return _FALLBACK_CONFIG_PATH


def _default_secret_path() -> Path:
    if _web_api is not None:
        return Path(_web_api.DEFAULT_SECRET_PATH)
    return _FALLBACK_SECRET_PATH


def _default_db_path() -> Path:
    if _web_api is not None:
        return Path(_web_api.DEFAULT_DB_PATH)
    return _FALLBACK_DB_PATH


@dataclass
class ApiDeps:
    """Per-process wiring for the HTTP handlers.

    Paths default to the canonical locations from ``web_api`` (or the
    identical local fallbacks when the peer module is absent). Stores are
    built FRESH per request from these paths (cheap local JSON reads), so
    config edits are visible without a restart. The background ``queue``
    object cannot be rebuilt per call -- it is accepted as an optional
    shared object (default None means queue UI disabled).
    """

    config_path: str | Path = field(default_factory=_default_config_path)
    secret_path: str | Path = field(default_factory=_default_secret_path)
    db_path: str | Path = field(default_factory=_default_db_path)
    client_factory: Callable[[str], Any] | None = None
    queue: Any | None = None


def _error_envelope(exc: BaseException) -> dict[str, Any]:
    """Build the token-free ``{"ok": False, "error": ...}`` envelope."""
    try:
        message = str(exc)
    except Exception:
        message = ""
    if not message:
        message = type(exc).__name__
    return {"ok": False, "error": redact(message)}


def _load_config(config_path: str | Path) -> ConfigStore:
    return ConfigStore(config_path)


def _load_secrets(secret_path: str | Path) -> FileSecretStore:
    return FileSecretStore(secret_path)


# ---------------------------------------------------------------------------
# Pure logic functions (no aiohttp, no globals). Each returns a JSON-safe
# dict and never raises: ValueError (bad input) and unexpected Exceptions
# both become {"ok": False, "error": str}.
# ---------------------------------------------------------------------------


def api_accounts(config: ConfigStore, secrets: FileSecretStore) -> dict[str, Any]:
    """List configured accounts with a has_token flag (never the token)."""
    try:
        if _web_api is not None:
            infos = _web_api.list_accounts(config, secrets)
            accounts = [
                {
                    "id": info.id,
                    "name": info.name,
                    "has_token": bool(info.has_token),
                }
                for info in infos
            ]
        else:
            accounts = [
                {
                    "id": account.id,
                    "name": account.name,
                    "has_token": _has_token(secrets, account.id),
                }
                for account in config.list_accounts()
            ]
        return {"ok": True, "accounts": accounts}
    except ValueError as exc:
        return _error_envelope(exc)
    except Exception as exc:
        return _error_envelope(exc)


def _has_token(secrets: FileSecretStore, account_id: str) -> bool:
    try:
        secrets.get_token(account_id)
    except Exception:
        return False
    return True


def api_destinations(
    config: ConfigStore, account_id: str | None = None
) -> dict[str, Any]:
    """List destinations, optionally filtered to one account.

    ``account_id`` of None or "" means no filter. An unknown, non-empty
    ``account_id`` is a ValueError (client-visible error envelope).
    """
    try:
        if account_id is not None and not isinstance(account_id, str):
            raise ValueError("account_id must be a string or null")
        if _web_api is not None:
            infos = _web_api.list_destinations(
                config, account_id if account_id else None
            )
            destinations = [
                {"id": info.id, "name": info.name, "account_id": info.account_id}
                for info in infos
            ]
        else:
            if account_id:
                try:
                    config.get_account(account_id)
                except (KeyError, ValueError) as exc:
                    raise ValueError(f"unknown account id '{account_id}'") from exc
            destinations = [
                {"id": dest.id, "name": dest.name, "account_id": dest.account_id}
                for dest in config.list_destinations()
                if not account_id or dest.account_id == account_id
            ]
        return {"ok": True, "destinations": destinations}
    except ValueError as exc:
        return _error_envelope(exc)
    except Exception as exc:
        return _error_envelope(exc)


def api_test_connection(
    config: ConfigStore,
    secrets: FileSecretStore,
    client_factory: Callable[[str], Any] | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate a bot token via getMe (never raises; token never echoed)."""
    try:
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        account_id = payload.get("account_id", "")
        destination_id = payload.get("destination_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError(
                "account_id must be a non-empty string; pick a configured "
                "Telegram account first."
            )
        if destination_id is not None and (
            not isinstance(destination_id, str) or not destination_id.strip()
        ):
            raise ValueError("destination_id must be a non-empty string or omitted")
        factory = client_factory if client_factory is not None else TelegramClient
        if _web_api is not None:
            result = _web_api.test_connection(
                config, secrets, factory, account_id, destination_id
            )
            return {
                "ok": bool(result.ok),
                "bot_username": result.bot_username,
                "error": result.error,
            }
        return _local_test_connection(config, secrets, factory, account_id, destination_id)
    except ValueError as exc:
        return _error_envelope(exc)
    except Exception as exc:
        return _error_envelope(exc)


def _local_test_connection(
    config: ConfigStore,
    secrets: FileSecretStore,
    client_factory: Callable[[str], Any],
    account_id: str,
    destination_id: str | None,
) -> dict[str, Any]:
    """Fallback for ``web_api.test_connection`` with identical semantics."""
    try:
        try:
            account = config.get_account(account_id)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown account id '{account_id}'") from exc
        if destination_id:
            try:
                dest = config.get_destination(destination_id)
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    f"unknown destination id '{destination_id}'"
                ) from exc
            if dest.account_id != account.id:
                raise ValueError(
                    f"destination '{destination_id}' belongs to account "
                    f"'{dest.account_id}', not '{account_id}'."
                )
        try:
            token = config.resolve_token(account.id, secrets)
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"no token stored for account id '{account_id}'; add it to "
                "the secret store first."
            ) from exc
        try:
            info = client_factory(token).validate_token()
        except Exception as exc:
            return {"ok": False, "bot_username": None, "error": redact(str(exc))}
        username = getattr(info, "username", None)
        return {"ok": True, "bot_username": username, "error": None}
    except ValueError as exc:
        return _error_envelope(exc)
    except Exception as exc:
        return _error_envelope(exc)


def api_queue_status(queue: Any | None) -> dict[str, Any]:
    """Expose background-queue state; None (no shared queue) disables it."""
    try:
        if queue is None:
            return {"ok": True, "enabled": False}
        if _web_api is not None:
            status = _web_api.queue_status(queue)
            return {"ok": True, "enabled": True, **dict(status)}
        snapshot = queue.status()
        try:
            fields = asdict(snapshot)
        except Exception:
            fields = {
                key: getattr(snapshot, key)
                for key in (
                    "queued",
                    "sending",
                    "success",
                    "failed",
                    "worker_running",
                    "buffered",
                    "maxsize",
                )
                if hasattr(snapshot, key)
            }
        return {"ok": True, "enabled": True, **fields}
    except ValueError as exc:
        return _error_envelope(exc)
    except Exception as exc:
        return _error_envelope(exc)


def _coerce_limit(limit: Any) -> int:
    """Validate a jobs limit; ValueError reads as the error envelope."""
    if isinstance(limit, bool):
        raise ValueError("limit must be a positive int")
    if isinstance(limit, str):
        try:
            limit = int(limit.strip())
        except (TypeError, ValueError):
            raise ValueError(
                f"limit must be a positive int, got {limit!r}"
            ) from None
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError(f"limit must be a positive int, got {limit!r}")
    return limit


def _job_to_dict(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "destination_id": job.destination_id,
        "status": job.status,
        "image_hash": job.image_hash,
        "filename": job.filename,
        "caption": job.caption,
        "attempts": job.attempts,
        "telegram_message_id": job.telegram_message_id,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def api_jobs(job_repo: Any, limit: Any = 50) -> dict[str, Any]:
    """Return the newest publish jobs (newest first); never raises."""
    try:
        count = _coerce_limit(limit)
        if _web_api is not None:
            jobs = _web_api.recent_jobs(job_repo, count)
            return {"ok": True, "jobs": [dict(job) for job in jobs]}
        rows = job_repo.list_recent(count)
        return {"ok": True, "jobs": [_job_to_dict(job) for job in rows]}
    except ValueError as exc:
        return _error_envelope(exc)
    except Exception as exc:
        return _error_envelope(exc)


# ---------------------------------------------------------------------------
# aiohttp registration (lazy import; decorator-call style for
# PromptServer.instance.routes, which is a RouteTableDef -- verified
# against ComfyUI server.py: ``routes = web.RouteTableDef()``).
# ---------------------------------------------------------------------------

ROUTE_PATHS = (
    ("GET", "/telegram_publisher/accounts"),
    ("GET", "/telegram_publisher/destinations"),
    ("POST", "/telegram_publisher/test_connection"),
    ("GET", "/telegram_publisher/queue_status"),
    ("GET", "/telegram_publisher/jobs"),
)


def _add_route(routes: Any, method: str, path: str, handler: Any) -> None:
    """Register one handler on a RouteTableDef-style or test-fake routes.

    Primary style is the decorator call ``routes.get(path)(handler)`` /
    ``routes.post(path)(handler)`` (what ``web.RouteTableDef`` supports).
    A plain ``routes.get(path, handler)`` fallback covers duck-typed
    fakes that take both arguments at once.
    """
    adder = getattr(routes, method.lower())
    try:
        decorator = adder(path)
    except TypeError:
        adder(path, handler)
        return
    if callable(decorator):
        decorator(handler)
        return
    adder(path, handler)


def register_routes(routes: Any, deps: ApiDeps | None = None) -> None:
    """Attach the Telegram Publisher HTTP API to ComfyUI's routes table.

    ``routes`` is duck-typed with ``.get``/``.post`` decorator factories
    (``PromptServer.instance.routes`` in ComfyUI). ``aiohttp`` is imported
    lazily inside the response helper (never at module or registration
    time), so this function -- and the whole module -- works without
    aiohttp installed; only serving a real request needs it.
    """
    resolved = deps if deps is not None else ApiDeps()

    def _respond(data: dict[str, Any]) -> Any:
        try:
            from aiohttp import web

            return web.json_response(data)
        except ImportError:
            return data  # No aiohttp (tests/standalone): raw JSON-safe dict.

    def _config() -> ConfigStore:
        return _load_config(resolved.config_path)

    def _secrets() -> FileSecretStore:
        return _load_secrets(resolved.secret_path)

    def _job_repo() -> PublishJobRepository:
        db = Database(resolved.db_path)
        try:
            db.init_schema()  # Fresh installs read [] instead of an error.
        except Exception:
            pass
        return PublishJobRepository(db)

    def _factory() -> Callable[[str], Any]:
        return resolved.client_factory if resolved.client_factory is not None else TelegramClient

    async def handle_accounts(request: Any) -> Any:
        return _respond(api_accounts(_config(), _secrets()))

    async def handle_destinations(request: Any) -> Any:
        try:
            query = request.rel_url.query
        except Exception:
            try:
                query = request.query
            except Exception:
                query = {}
        try:
            account_id = query.get("account_id")
        except Exception:
            account_id = None
        return _respond(api_destinations(_config(), account_id))

    async def handle_test_connection(request: Any) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return _respond(
            api_test_connection(_config(), _secrets(), _factory(), payload)
        )

    async def handle_queue_status(request: Any) -> Any:
        return _respond(api_queue_status(resolved.queue))

    async def handle_jobs(request: Any) -> Any:
        try:
            query = request.rel_url.query
        except Exception:
            try:
                query = request.query
            except Exception:
                query = {}
        try:
            limit = query.get("limit", 50)
        except Exception:
            limit = 50
        return _respond(api_jobs(_job_repo(), limit))

    _add_route(routes, "GET", "/telegram_publisher/accounts", handle_accounts)
    _add_route(routes, "GET", "/telegram_publisher/destinations", handle_destinations)
    _add_route(routes, "POST", "/telegram_publisher/test_connection", handle_test_connection)
    _add_route(routes, "GET", "/telegram_publisher/queue_status", handle_queue_status)
    _add_route(routes, "GET", "/telegram_publisher/jobs", handle_jobs)
