"""Server-side business logic for the Epic 5 settings/status UI (T051-T054).

This module owns everything the HTTP routes + frontend need EXCEPT the
routes themselves (peer-owned ``publisher_nodes/routes.py``) and the
frontend (peer-owned ``web/settings.js``):

- T051 account selector data: :func:`list_accounts` / :func:`account_ids`.
- T052 destination selector data: :func:`list_destinations` /
  :func:`destination_ids`.
- T053 connection test: :func:`test_connection`.
- T054 status: :func:`queue_status` / :func:`recent_jobs`.

Rules:

- Stdlib + existing layers only; no ComfyUI imports; no network here
  (``test_connection`` performs exactly one ``validate_token`` call
  through an injected ``client_factory``).
- EVERYTHING returned is JSON-serializable (dataclasses, dicts, lists,
  strs, bools, ints, None). Tokens and chat contents never appear in
  outputs: :class:`DestinationInfo` deliberately has no ``chat_id`` and
  :class:`ConnectionResult` carries only a username.
- UI-facing error convention: bad ids raise ``ValueError`` with an
  actionable message. ``ConfigStore``/``FileSecretStore`` raise
  ``KeyError`` for unknown ids; this module catches ``KeyError`` (and
  ``ValueError`` from validation) at the boundary and re-raises
  ``ValueError(str)`` so the UI layer speaks one error type.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from storage.config import ConfigStore, SecretStore
from storage.repositories import PublishJobRepository
from telegram.errors import (
    AuthenticationError,
    RateLimitError,
    TransientTelegramError,
    redact,
)

# Extension dir derived from inside publisher_nodes/ (same pattern as
# publisher_nodes/send_image.py).
_EXTENSION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = _EXTENSION_DIR / "user_config" / "accounts.json"
DEFAULT_SECRET_PATH = _EXTENSION_DIR / "secrets" / "tokens.json"
DEFAULT_DB_PATH = _EXTENSION_DIR / "history" / "publisher.sqlite3"


# ---------------------------------------------------------------------------
# Data models (all JSON-serializable, token-free by construction)
# ---------------------------------------------------------------------------


@dataclass
class AccountInfo:
    """One account for the T051 selector. ``has_token`` only, never the value."""

    id: str
    name: str
    has_token: bool


@dataclass
class DestinationInfo:
    """One destination for the T052 selector. No ``chat_id`` (private)."""

    id: str
    name: str
    account_id: str


@dataclass
class ConnectionResult:
    """Outcome of :func:`test_connection`. Never raises; never carries a token."""

    ok: bool
    bot_username: str | None
    error: str | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _map_store_error(exc: BaseException) -> ValueError:
    """Map a store ``KeyError``/``ValueError`` to a UI-facing ``ValueError``."""
    return ValueError(str(exc))


# ---------------------------------------------------------------------------
# T051 / T052 selectors
# ---------------------------------------------------------------------------


def list_accounts(config: ConfigStore, secrets: SecretStore) -> list[AccountInfo]:
    """Return one :class:`AccountInfo` per configured account.

    ``has_token`` is probed via ``secrets.get_token``; the token value is
    never read into a returned structure.
    """
    try:
        accounts = config.list_accounts()
    except (KeyError, ValueError) as exc:
        raise _map_store_error(exc) from exc
    infos: list[AccountInfo] = []
    for account in accounts:
        try:
            secrets.get_token(account.id)
        except Exception:
            infos.append(AccountInfo(id=account.id, name=account.name, has_token=False))
        else:
            infos.append(AccountInfo(id=account.id, name=account.name, has_token=True))
    return infos


def list_destinations(
    config: ConfigStore, account_id: str | None = None
) -> list[DestinationInfo]:
    """Return destinations (names only), optionally filtered by account.

    Raises:
        ValueError: If ``account_id`` is empty/blank or unknown.
    """
    try:
        if account_id is not None:
            if not isinstance(account_id, str) or not account_id.strip():
                raise ValueError("account_id must be a non-empty string")
            # Existence check: unknown filter -> ValueError (mapped below).
            config.get_account(account_id)
            destinations = [
                d for d in config.list_destinations() if d.account_id == account_id
            ]
        else:
            destinations = config.list_destinations()
    except KeyError as exc:
        raise _map_store_error(exc) from exc
    return [
        DestinationInfo(id=d.id, name=d.name, account_id=d.account_id)
        for d in destinations
    ]


def account_ids(config: ConfigStore) -> list[str]:
    """Sorted account ids for COMBO widgets."""
    try:
        ids = [a.id for a in config.list_accounts()]
    except (KeyError, ValueError) as exc:
        raise _map_store_error(exc) from exc
    return sorted(ids)


def destination_ids(
    config: ConfigStore, account_id: str | None = None
) -> list[str]:
    """Sorted destination ids for COMBO widgets, optionally filtered.

    Raises:
        ValueError: If ``account_id`` is empty/blank or unknown.
    """
    return sorted(d.id for d in list_destinations(config, account_id))


# ---------------------------------------------------------------------------
# T053 connection test
# ---------------------------------------------------------------------------


def test_connection(
    config: ConfigStore,
    secrets: SecretStore,
    client_factory: Callable[[str], Any],
    account_id: str,
    destination_id: str | None = None,
) -> ConnectionResult:
    """Validate one account's token via ``validate_token``. Never raises.

    ``client_factory`` signature: ``(token: str) -> TelegramClient`` (the
    same factory shape the nodes use). Only ``validate_token()`` is called.

    Every failure mode returns ``ok=False`` with an actionable ``error``
    string; the token value never appears in any output.
    """
    if not isinstance(account_id, str) or not account_id.strip():
        return ConnectionResult(
            ok=False,
            bot_username=None,
            error="unknown account: account_id must be a non-empty string; "
            "pick an account from the account selector.",
        )
    try:
        account = config.get_account(account_id)
    except (KeyError, ValueError) as exc:
        return ConnectionResult(
            ok=False,
            bot_username=None,
            error=redact(
                f"unknown account id '{account_id}': {exc}; check "
                "user_config/accounts.json and pick an account from the "
                "account selector."
            ),
        )
    if destination_id is not None:
        if not isinstance(destination_id, str) or not destination_id.strip():
            return ConnectionResult(
                ok=False,
                bot_username=None,
                error="unknown destination: destination_id must be a "
                "non-empty string; pick a destination from the selector.",
            )
        try:
            dest = config.get_destination(destination_id)
        except (KeyError, ValueError) as exc:
            return ConnectionResult(
                ok=False,
                bot_username=None,
                error=redact(
                    f"unknown destination id '{destination_id}': {exc}; pick "
                    "a destination from the destination selector."
                ),
            )
        if dest.account_id != account.id:
            return ConnectionResult(
                ok=False,
                bot_username=None,
                error=(
                    f"destination '{destination_id}' belongs to account "
                    f"'{dest.account_id}', not '{account.id}'; select a "
                    "destination that belongs to the tested account."
                ),
            )
    try:
        token = config.resolve_token(account.id, secrets)
    except (KeyError, ValueError) as exc:
        return ConnectionResult(
            ok=False,
            bot_username=None,
            error=redact(
                f"no token stored for account '{account.id}': {exc}; add it "
                "to secrets/tokens.json (never paste the token into the "
                "workflow), then test again."
            ),
        )
    try:
        client = client_factory(token)
    except Exception as exc:
        return ConnectionResult(
            ok=False,
            bot_username=None,
            error=redact(f"could not create Telegram client: {exc}"),
        )
    try:
        bot = client.validate_token()
    except AuthenticationError:
        return ConnectionResult(
            ok=False,
            bot_username=None,
            error=(
                "bot token invalid: rejected by Telegram. Check "
                "secrets/tokens.json for this account (never paste the "
                "token into the workflow), then test again."
            ),
        )
    except RateLimitError as exc:
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            detail = (
                "Telegram rate limit hit; retry automatically after "
                f"{retry_after}s (the queue handles this in background mode)."
            )
        else:
            detail = (
                "Telegram rate limit hit; retry automatically shortly "
                "(the queue handles this in background mode)."
            )
        return ConnectionResult(ok=False, bot_username=None, error=redact(detail))
    except TransientTelegramError as exc:
        return ConnectionResult(
            ok=False,
            bot_username=None,
            error=redact(
                f"Telegram reachable but request failed: {exc}; safe to "
                "retry without regenerating the image."
            ),
        )
    except Exception as exc:
        return ConnectionResult(
            ok=False,
            bot_username=None,
            error=redact(f"Telegram connection test failed: {exc}"),
        )
    try:
        username = getattr(bot, "username", None)
    except Exception:
        username = None
    return ConnectionResult(ok=True, bot_username=username, error=None)


# ---------------------------------------------------------------------------
# T054 status
# ---------------------------------------------------------------------------


def queue_status(queue: Any | None) -> dict:
    """Return a JSON-safe queue snapshot.

    ``None`` (background mode off) -> ``{"enabled": False}``; otherwise
    ``{"enabled": True, **asdict(queue.status())}`` where the status is the
    :class:`services.queue.QueueStatus` snapshot.
    """
    if queue is None:
        return {"enabled": False}
    return {"enabled": True, **asdict(queue.status())}


def recent_jobs(job_repo: PublishJobRepository, limit: int = 20) -> list[dict]:
    """Return up to ``limit`` newest jobs as JSON-safe dicts.

    Rows contain all ``publish_jobs`` columns (incl. caption/message ids);
    no tokens exist in job rows by construction.

    Raises:
        ValueError: If ``limit`` is not an int in 1..100.
    """
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 100
    ):
        raise ValueError("limit must be an int in 1..100")
    return [asdict(job) for job in job_repo.list_recent(limit)]
