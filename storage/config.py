"""Local account/destination configuration (T007).

Secrets (bot tokens) live ONLY in a ``SecretStore`` (e.g. a separate
restricted-permission JSON file). The JSON config file managed by
``ConfigStore`` holds accounts + destinations and must never contain tokens.

The ComfyUI node layer (T009) references accounts/destinations by ID only
and resolves tokens via ``ConfigStore.resolve_token``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Account:
    """A Telegram bot account reference. No token field by design (FR-001)."""

    id: str
    name: str


@dataclass
class Destination:
    """A named publish target.

    ``chat_id`` is ALWAYS a string so numeric IDs and @usernames both work
    (FR-002). Callers must stringify numeric IDs before constructing.
    """

    id: str
    name: str
    account_id: str
    chat_id: str


# ---------------------------------------------------------------------------
# Secret store abstraction
# ---------------------------------------------------------------------------


class SecretStore(Protocol):
    """Provider interface so an OS keychain backend can replace the file store."""

    def get_token(self, account_id: str) -> str: ...
    def set_token(self, account_id: str, token: str) -> None: ...
    def delete_token(self, account_id: str) -> None: ...


class FileSecretStore:
    """Persist tokens in a separate JSON file with owner-only permissions.

    File format: ``{account_id: token}``.
    """

    def __init__(self, secret_path: str | Path) -> None:
        self._path = Path(secret_path)
        self._tokens: dict[str, str] = {}
        self._load()

    @property
    def secret_path(self) -> Path:
        return self._path

    # -- public API ------------------------------------------------------

    def get_token(self, account_id: str) -> str:
        _require_non_empty_str(account_id, "account_id")
        try:
            return self._tokens[account_id]
        except KeyError:
            raise KeyError(f"no token stored for account id '{account_id}'") from None

    def set_token(self, account_id: str, token: str) -> None:
        _require_non_empty_str(account_id, "account_id")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token must be a non-empty string")
        self._tokens[account_id] = token
        self._save()

    def delete_token(self, account_id: str) -> None:
        _require_non_empty_str(account_id, "account_id")
        try:
            del self._tokens[account_id]
        except KeyError:
            raise KeyError(f"no token stored for account id '{account_id}'") from None
        self._save()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            self._tokens = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"secret file '{self._path}' is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(
                f"secret file '{self._path}' must contain a JSON object"
            )
        tokens: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"secret file '{self._path}' has an invalid account id")
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"secret file '{self._path}' has an invalid token entry "
                    f"for account id '{key}'"
                )
            tokens[key] = value
        self._tokens = tokens

    def _save(self) -> None:
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._tokens, indent=2, sort_keys=True), encoding="utf-8"
        )
        _restrict_to_owner(self._path)


def _restrict_to_owner(path: Path) -> None:
    """Set owner-only (0600) permissions; best-effort on Windows."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        if os.name != "nt":
            raise


# ---------------------------------------------------------------------------
# Config store (no secrets)
# ---------------------------------------------------------------------------


class ConfigStore:
    """Load/save accounts + destinations from a JSON config file (no secrets)."""

    def __init__(self, config_path: str | Path) -> None:
        self._path = Path(config_path)
        self._accounts: dict[str, Account] = {}
        self._destinations: dict[str, Destination] = {}
        self._load()

    @property
    def config_path(self) -> Path:
        return self._path

    # -- accounts --------------------------------------------------------

    def add_account(self, account: Account) -> Account:
        _validate_account(account)
        if account.id in self._accounts:
            raise ValueError(f"account id '{account.id}' already exists")
        self._accounts[account.id] = Account(id=account.id, name=account.name)
        self._save()
        return self._accounts[account.id]

    def get_account(self, account_id: str) -> Account:
        _require_non_empty_str(account_id, "account_id")
        try:
            account = self._accounts[account_id]
        except KeyError:
            raise KeyError(f"unknown account id '{account_id}'") from None
        return Account(id=account.id, name=account.name)

    def remove_account(self, account_id: str) -> None:
        _require_non_empty_str(account_id, "account_id")
        if account_id not in self._accounts:
            raise KeyError(f"unknown account id '{account_id}'")
        blockers = sorted(
            d.id for d in self._destinations.values() if d.account_id == account_id
        )
        if blockers:
            raise ValueError(
                f"cannot remove account '{account_id}': "
                f"still referenced by destination(s) {', '.join(blockers)}; "
                f"remove those destinations first"
            )
        del self._accounts[account_id]
        self._save()

    def list_accounts(self) -> list[Account]:
        return [Account(id=a.id, name=a.name) for a in self._accounts.values()]

    # -- destinations ----------------------------------------------------

    def add_destination(self, destination: Destination) -> Destination:
        _validate_destination(destination)
        if destination.id in self._destinations:
            raise ValueError(f"destination id '{destination.id}' already exists")
        if destination.account_id not in self._accounts:
            raise ValueError(
                f"destination '{destination.id}' references unknown account "
                f"id '{destination.account_id}'"
            )
        self._destinations[destination.id] = Destination(
            id=destination.id,
            name=destination.name,
            account_id=destination.account_id,
            chat_id=destination.chat_id,
        )
        self._save()
        return self._destinations[destination.id]

    def get_destination(self, destination_id: str) -> Destination:
        _require_non_empty_str(destination_id, "destination_id")
        try:
            dest = self._destinations[destination_id]
        except KeyError:
            raise KeyError(f"unknown destination id '{destination_id}'") from None
        return Destination(
            id=dest.id, name=dest.name, account_id=dest.account_id, chat_id=dest.chat_id
        )

    def remove_destination(self, destination_id: str) -> None:
        _require_non_empty_str(destination_id, "destination_id")
        if destination_id not in self._destinations:
            raise KeyError(f"unknown destination id '{destination_id}'")
        del self._destinations[destination_id]
        self._save()

    def list_destinations(self) -> list[Destination]:
        return [
            Destination(
                id=d.id, name=d.name, account_id=d.account_id, chat_id=d.chat_id
            )
            for d in self._destinations.values()
        ]

    # -- token resolution -------------------------------------------------

    def resolve_token(self, account_id: str, secret_store: SecretStore) -> str:
        """Return the bot token for an account id (node layer uses IDs only)."""
        _require_non_empty_str(account_id, "account_id")
        if account_id not in self._accounts:
            raise KeyError(f"unknown account id '{account_id}'")
        return secret_store.get_token(account_id)

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            self._accounts = {}
            self._destinations = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"config file '{self._path}' is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(f"config file '{self._path}' must contain a JSON object")
        accounts_raw = raw.get("accounts", [])
        destinations_raw = raw.get("destinations", [])
        if not isinstance(accounts_raw, list) or not isinstance(
            destinations_raw, list
        ):
            raise ValueError(
                f"config file '{self._path}' must contain "
                f"'accounts' and 'destinations' lists"
            )
        accounts: dict[str, Account] = {}
        for entry in accounts_raw:
            account = _account_from_dict(entry, str(self._path))
            if account.id in accounts:
                raise ValueError(
                    f"config file '{self._path}' has duplicate account "
                    f"id '{account.id}'"
                )
            accounts[account.id] = account
        destinations: dict[str, Destination] = {}
        for entry in destinations_raw:
            dest = _destination_from_dict(entry, str(self._path))
            if dest.id in destinations:
                raise ValueError(
                    f"config file '{self._path}' has duplicate destination "
                    f"id '{dest.id}'"
                )
            if dest.account_id not in accounts:
                raise ValueError(
                    f"config file '{self._path}': destination '{dest.id}' "
                    f"references unknown account id '{dest.account_id}'"
                )
            destinations[dest.id] = dest
        self._accounts = accounts
        self._destinations = destinations

    def _save(self) -> None:
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "accounts": [asdict(a) for a in self._accounts.values()],
            "destinations": [asdict(d) for d in self._destinations.values()],
        }
        self._path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_non_empty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _validate_account(account: Account) -> None:
    if not isinstance(account, Account):
        raise ValueError("account must be an Account instance")
    _require_non_empty_str(account.id, "account.id")
    _require_non_empty_str(account.name, "account.name")


def _validate_destination(destination: Destination) -> None:
    if not isinstance(destination, Destination):
        raise ValueError("destination must be a Destination instance")
    _require_non_empty_str(destination.id, "destination.id")
    _require_non_empty_str(destination.name, "destination.name")
    _require_non_empty_str(destination.account_id, "destination.account_id")
    if not isinstance(destination.chat_id, str) or not destination.chat_id.strip():
        raise ValueError(
            "destination.chat_id must be a non-empty string "
            "(use a string for numeric IDs and @usernames)"
        )


def _account_from_dict(entry: object, source: str) -> Account:
    if not isinstance(entry, dict):
        raise ValueError(f"config file '{source}' has an invalid account entry")
    account = Account(id=entry.get("id", ""), name=entry.get("name", ""))
    _validate_account(account)
    return account


def _destination_from_dict(entry: object, source: str) -> Destination:
    if not isinstance(entry, dict):
        raise ValueError(f"config file '{source}' has an invalid destination entry")
    dest = Destination(
        id=entry.get("id", ""),
        name=entry.get("name", ""),
        account_id=entry.get("account_id", ""),
        chat_id=entry.get("chat_id", ""),
    )
    _validate_destination(dest)
    return dest
