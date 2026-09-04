"""T007 tests: local account/destination configuration with secret separation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from storage.config import (
    Account,
    ConfigStore,
    Destination,
    FileSecretStore,
)


# Fake tokens only — never real credentials.
TOKEN_A = "TEST_TOKEN_aaa"
TOKEN_B = "TEST_TOKEN_bbb"


def make_store(tmp_path: Path) -> tuple[ConfigStore, FileSecretStore]:
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    return config, secrets


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------


def test_account_crud_happy_path(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    assert config.get_account("acc1") == Account(id="acc1", name="Main bot")
    assert config.list_accounts() == [Account(id="acc1", name="Main bot")]
    config.remove_account("acc1")
    assert config.list_accounts() == []
    with pytest.raises(KeyError):
        config.get_account("acc1")


def test_account_persists_across_reload(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    reloaded = ConfigStore(tmp_path / "config.json")
    assert reloaded.get_account("acc1") == Account(id="acc1", name="Main bot")


def test_account_rejects_empty_id_or_name(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    with pytest.raises(ValueError):
        config.add_account(Account(id="", name="x"))
    with pytest.raises(ValueError):
        config.add_account(Account(id="   ", name="x"))
    with pytest.raises(ValueError):
        config.add_account(Account(id="a", name=""))
    with pytest.raises(ValueError):
        config.add_account(Account(id="a", name="   "))


def test_account_rejects_duplicate_id(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="One"))
    with pytest.raises(ValueError, match="acc1"):
        config.add_account(Account(id="acc1", name="Two"))


def test_get_unknown_account_raises_keyerror(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    with pytest.raises(KeyError):
        config.get_account("nope")
    with pytest.raises(KeyError):
        config.remove_account("nope")


# ---------------------------------------------------------------------------
# Destination CRUD
# ---------------------------------------------------------------------------


def test_destination_crud_happy_path(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    config.add_destination(
        Destination(id="dst1", name="Channel", account_id="acc1", chat_id="-12345")
    )
    config.add_destination(
        Destination(id="dst2", name="User", account_id="acc1", chat_id="@someuser")
    )
    got = config.get_destination("dst1")
    assert got.chat_id == "-12345"
    assert isinstance(got.chat_id, str)
    assert {d.id for d in config.list_destinations()} == {"dst1", "dst2"}
    config.remove_destination("dst1")
    assert [d.id for d in config.list_destinations()] == ["dst2"]
    with pytest.raises(KeyError):
        config.get_destination("dst1")


def test_destination_rejects_unknown_account(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    with pytest.raises(ValueError, match="unknown account"):
        config.add_destination(
            Destination(id="d1", name="X", account_id="ghost", chat_id="123")
        )


def test_destination_rejects_empty_fields_and_non_string_chat_id(
    tmp_path: Path,
) -> None:
    config, _ = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    with pytest.raises(ValueError):
        config.add_destination(
            Destination(id="", name="X", account_id="acc1", chat_id="123")
        )
    with pytest.raises(ValueError):
        config.add_destination(
            Destination(id="d1", name="", account_id="acc1", chat_id="123")
        )
    with pytest.raises(ValueError):
        config.add_destination(
            Destination(id="d1", name="X", account_id="", chat_id="123")
        )
    with pytest.raises(ValueError):
        config.add_destination(
            Destination(id="d1", name="X", account_id="acc1", chat_id="")
        )
    with pytest.raises(ValueError):
        # Numeric chat IDs must be passed as strings (FR-002).
        config.add_destination(
            Destination(id="d1", name="X", account_id="acc1", chat_id=12345)  # type: ignore[arg-type]
        )


def test_destination_rejects_duplicate_id(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    config.add_destination(
        Destination(id="d1", name="A", account_id="acc1", chat_id="1")
    )
    with pytest.raises(ValueError, match="d1"):
        config.add_destination(
            Destination(id="d1", name="B", account_id="acc1", chat_id="2")
        )


def test_get_remove_unknown_destination_raises_keyerror(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    with pytest.raises(KeyError):
        config.get_destination("nope")
    with pytest.raises(KeyError):
        config.remove_destination("nope")


# ---------------------------------------------------------------------------
# Orphan protection
# ---------------------------------------------------------------------------


def test_remove_account_with_destinations_fails(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    config.add_destination(
        Destination(id="d1", name="A", account_id="acc1", chat_id="1")
    )
    with pytest.raises(ValueError, match="d1"):
        config.remove_account("acc1")
    # Account must still exist; no orphaned destination removal happened.
    assert config.get_account("acc1").id == "acc1"
    assert config.get_destination("d1").account_id == "acc1"
    # After removing the destination, account removal succeeds.
    config.remove_destination("d1")
    config.remove_account("acc1")
    assert config.list_accounts() == []


# ---------------------------------------------------------------------------
# Secrets + token resolution
# ---------------------------------------------------------------------------


def test_secret_store_crud_and_reload(tmp_path: Path) -> None:
    _, secrets = make_store(tmp_path)
    secrets.set_token("acc1", TOKEN_A)
    assert secrets.get_token("acc1") == TOKEN_A
    secrets.set_token("acc1", TOKEN_B)
    assert secrets.get_token("acc1") == TOKEN_B
    reloaded = FileSecretStore(tmp_path / "secrets.json")
    assert reloaded.get_token("acc1") == TOKEN_B
    secrets.delete_token("acc1")
    with pytest.raises(KeyError):
        secrets.get_token("acc1")


def test_secret_store_unknown_id_keyerror_names_id(tmp_path: Path) -> None:
    _, secrets = make_store(tmp_path)
    with pytest.raises(KeyError, match="ghost-acc"):
        secrets.get_token("ghost-acc")
    with pytest.raises(KeyError, match="ghost-acc"):
        secrets.delete_token("ghost-acc")


def test_secret_store_rejects_empty_token_or_id(tmp_path: Path) -> None:
    _, secrets = make_store(tmp_path)
    with pytest.raises(ValueError):
        secrets.set_token("", TOKEN_A)
    with pytest.raises(ValueError):
        secrets.set_token("acc1", "")
    with pytest.raises(ValueError):
        secrets.set_token("acc1", "   ")


def test_secret_keyerror_contains_no_token_content(tmp_path: Path) -> None:
    _, secrets = make_store(tmp_path)
    secrets.set_token("acc1", TOKEN_A)
    try:
        secrets.get_token("ghost-acc")
        raise AssertionError("expected KeyError")
    except KeyError as exc:
        assert TOKEN_A not in str(exc)


def test_resolve_token_happy_path(tmp_path: Path) -> None:
    config, secrets = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    secrets.set_token("acc1", TOKEN_A)
    assert config.resolve_token("acc1", secrets) == TOKEN_A


def test_resolve_token_unknown_account_or_missing_token(tmp_path: Path) -> None:
    config, secrets = make_store(tmp_path)
    with pytest.raises(KeyError):
        config.resolve_token("ghost", secrets)
    config.add_account(Account(id="acc1", name="Main bot"))
    with pytest.raises(KeyError, match="acc1"):
        config.resolve_token("acc1", secrets)


# ---------------------------------------------------------------------------
# Separation: token must never be in the config file
# ---------------------------------------------------------------------------


def test_config_file_never_contains_token(tmp_path: Path) -> None:
    config, secrets = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    config.add_destination(
        Destination(id="d1", name="Chan", account_id="acc1", chat_id="@chan")
    )
    secrets.set_token("acc1", TOKEN_A)
    raw = (tmp_path / "config.json").read_bytes()
    assert TOKEN_A.encode() not in raw
    assert b"TEST_TOKEN" not in raw
    parsed = json.loads(raw.decode("utf-8"))
    assert "TEST_TOKEN" not in json.dumps(parsed)


def test_secret_file_does_not_hold_config_names(tmp_path: Path) -> None:
    config, secrets = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    secrets.set_token("acc1", TOKEN_A)
    secret_raw = (tmp_path / "secrets.json").read_bytes()
    assert TOKEN_A.encode() in secret_raw
    assert b"Main bot" not in secret_raw


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def test_secret_file_owner_only_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX-only permission check")
    _, secrets = make_store(tmp_path)
    secrets.set_token("acc1", TOKEN_A)
    mode = stat.S_IMODE(os.stat(tmp_path / "secrets.json").st_mode)
    assert mode == 0o600
