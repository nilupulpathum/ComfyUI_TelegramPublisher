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
# Bot settings (T060 remote-control configuration)
# ---------------------------------------------------------------------------

#: Hosts a command-triggered HTTP call is allowed to target. Command
#: handlers (e.g. a future ``/run`` trigger) must never aim at remote
#: hosts from a config file; only loopback is accepted.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class Trigger:
    """A named remote-workflow trigger (resolved at ``/run`` time)."""

    name: str
    prompt_file: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("trigger.name must be a non-empty string")
        if not isinstance(self.prompt_file, str) or not self.prompt_file.strip():
            raise ValueError("trigger.prompt_file must be a non-empty string")


@dataclass
class BotSettings:
    """Remote-control settings (top-level ``"settings"`` object in config).

    ``admin_chat_ids`` is the explicit admin allowlist (FR-002 strings);
    ``comfy_host`` must stay loopback so command-triggered HTTP can never
    be aimed at remote hosts via a config file.
    """

    review_mode: bool = False
    admin_chat_ids: tuple[str, ...] = ()
    comfy_host: str = "127.0.0.1"
    comfy_port: int = 8188
    triggers: tuple[Trigger, ...] = ()


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
        self._settings: BotSettings = BotSettings()
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

    # -- bot settings (T060) ----------------------------------------------

    def get_settings(self) -> BotSettings:
        """Return a copy of the remote-control settings (defaults if unset)."""
        return BotSettings(
            review_mode=self._settings.review_mode,
            admin_chat_ids=tuple(self._settings.admin_chat_ids),
            comfy_host=self._settings.comfy_host,
            comfy_port=self._settings.comfy_port,
            triggers=tuple(
                Trigger(name=t.name, prompt_file=t.prompt_file)
                for t in self._settings.triggers
            ),
        )

    def save_settings(self, settings: BotSettings) -> BotSettings:
        """Validate, persist, and return normalized bot settings.

        Raises:
            ValueError: If any field has the wrong type or an illegal value
                (notably a non-loopback ``comfy_host``).
        """
        normalized = _normalize_settings(settings)
        self._settings = normalized
        self._save()
        return self.get_settings()

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
            self._settings = BotSettings()
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
        self._settings = _settings_from_raw(raw.get("settings"), str(self._path))

    def _save(self) -> None:
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "accounts": [asdict(a) for a in self._accounts.values()],
            "destinations": [asdict(d) for d in self._destinations.values()],
            "settings": _settings_to_dict(self._settings),
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


# ---------------------------------------------------------------------------
# Bot settings helpers (T060)
# ---------------------------------------------------------------------------

_SETTINGS_KEYS = frozenset(
    {"review_mode", "admin_chat_ids", "comfy_host", "comfy_port", "triggers"}
)


def _validate_admin_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError("admin_chat_ids must be a list of non-empty strings")
    ids: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("admin_chat_ids entries must be non-empty strings")
        ids.append(entry)
    return tuple(ids)


def _validate_comfy_host(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("comfy_host must be a non-empty string")
    host = value.strip()
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"comfy_host must be loopback {sorted(LOOPBACK_HOSTS)}; "
            f"refusing '{host}'"
        )
    return host


def _validate_comfy_port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("comfy_port must be an int in 1..65535")
    if not 1 <= value <= 65535:
        raise ValueError("comfy_port must be an int in 1..65535")
    return value


def _validate_triggers(value: object) -> tuple[Trigger, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError("triggers must be a list of Trigger entries")
    triggers: list[Trigger] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, Trigger):
            raise ValueError("triggers entries must be Trigger instances")
        # Trigger.__post_init__ already enforces non-empty name/prompt_file.
        if entry.name in seen:
            raise ValueError(
                f"duplicate trigger name '{entry.name}' (names must be unique)"
            )
        seen.add(entry.name)
        triggers.append(
            Trigger(name=entry.name, prompt_file=entry.prompt_file)
        )
    return tuple(triggers)


def _normalize_settings(settings: BotSettings) -> BotSettings:
    """Validate ``settings`` and return a normalized copy (tuples, copies)."""
    if not isinstance(settings, BotSettings):
        raise ValueError("settings must be a BotSettings instance")
    if not isinstance(settings.review_mode, bool):
        raise ValueError("review_mode must be a bool")
    return BotSettings(
        review_mode=settings.review_mode,
        admin_chat_ids=_validate_admin_ids(settings.admin_chat_ids),
        comfy_host=_validate_comfy_host(settings.comfy_host),
        comfy_port=_validate_comfy_port(settings.comfy_port),
        triggers=_validate_triggers(settings.triggers),
    )


def _settings_from_raw(raw: object, source: str) -> BotSettings:
    """Parse the top-level ``"settings"`` object; absent -> defaults."""
    if raw is None:
        return BotSettings()
    if not isinstance(raw, dict):
        raise ValueError(f"config file '{source}': 'settings' must be an object")
    unknown = set(raw.keys()) - _SETTINGS_KEYS
    if unknown:
        raise ValueError(
            f"config file '{source}': unknown settings key(s) "
            f"{sorted(str(k) for k in unknown)}"
        )
    triggers_raw = raw.get("triggers", ())
    if not isinstance(triggers_raw, (tuple, list)):
        raise ValueError(f"config file '{source}': 'triggers' must be a list")
    triggers: list[Trigger] = []
    for entry in triggers_raw:
        if not isinstance(entry, dict):
            raise ValueError(
                f"config file '{source}': trigger entries must be objects"
            )
        unknown_t = set(entry.keys()) - {"name", "prompt_file"}
        if unknown_t:
            raise ValueError(
                f"config file '{source}': unknown trigger key(s) "
                f"{sorted(str(k) for k in unknown_t)}"
            )
        triggers.append(
            Trigger(
                name=entry.get("name", ""),  # type: ignore[arg-type]
                prompt_file=entry.get("prompt_file", ""),  # type: ignore[arg-type]
            )
        )
    return _normalize_settings(
        BotSettings(
            review_mode=raw.get("review_mode", False),  # type: ignore[arg-type]
            admin_chat_ids=raw.get("admin_chat_ids", ()),  # type: ignore[arg-type]
            comfy_host=raw.get("comfy_host", "127.0.0.1"),  # type: ignore[arg-type]
            comfy_port=raw.get("comfy_port", 8188),  # type: ignore[arg-type]
            triggers=triggers,  # type: ignore[arg-type]
        )
    )


def _settings_to_dict(settings: BotSettings) -> dict:
    return {
        "review_mode": settings.review_mode,
        "admin_chat_ids": list(settings.admin_chat_ids),
        "comfy_host": settings.comfy_host,
        "comfy_port": settings.comfy_port,
        "triggers": [
            {"name": t.name, "prompt_file": t.prompt_file}
            for t in settings.triggers
        ],
    }
