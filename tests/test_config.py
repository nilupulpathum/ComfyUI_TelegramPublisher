"""T007 tests: local account/destination configuration with secret separation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from storage.config import (
    Account,
    BotSettings,
    ConfigStore,
    Destination,
    FileSecretStore,
    PromptTarget,
    PublishSpec,
    Trigger,
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


# ---------------------------------------------------------------------------
# Bot settings (T060 remote-control configuration; append-only additions)
# ---------------------------------------------------------------------------


def test_settings_default_when_absent(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    assert config.get_settings() == BotSettings()
    assert config.get_settings().admin_chat_ids == ()
    assert config.get_settings().comfy_host == "127.0.0.1"
    assert config.get_settings().comfy_port == 8188
    assert config.get_settings().review_mode is False
    assert config.get_settings().triggers == ()


def test_settings_save_get_round_trip_and_reload(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    saved = config.save_settings(
        BotSettings(
            review_mode=True,
            admin_chat_ids=["123", "@admin"],
            comfy_host="localhost",
            comfy_port=8199,
            triggers=[Trigger(name="t1", prompt_file="p1.json")],
        )
    )
    assert saved.admin_chat_ids == ("123", "@admin")
    assert saved.triggers == (Trigger(name="t1", prompt_file="p1.json"),)
    got = config.get_settings()
    assert got == saved
    reloaded = ConfigStore(tmp_path / "config.json")
    assert reloaded.get_settings() == saved


def test_settings_save_preserves_accounts_and_destinations(
    tmp_path: Path,
) -> None:
    config, _ = make_store(tmp_path)
    config.add_account(Account(id="acc1", name="Main bot"))
    config.add_destination(
        Destination(id="d1", name="Chan", account_id="acc1", chat_id="@chan")
    )
    config.save_settings(BotSettings(admin_chat_ids=["1"]))
    reloaded = ConfigStore(tmp_path / "config.json")
    assert reloaded.get_account("acc1") == Account(id="acc1", name="Main bot")
    assert reloaded.get_destination("d1").chat_id == "@chan"
    assert reloaded.get_settings().admin_chat_ids == ("1",)


def test_settings_get_returns_independent_copy(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    config.save_settings(
        BotSettings(
            admin_chat_ids=["1"],
            triggers=[Trigger(name="t", prompt_file="p.json")],
        )
    )
    first = config.get_settings()
    second = config.get_settings()
    assert first == second
    assert first.triggers[0] is not second.triggers[0]


def test_settings_rejects_bad_admin_ids(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    for bad_ids in ([""], ["   "], ["ok", ""], ["ok", 42], "123", "abc", 42):
        with pytest.raises(ValueError):
            config.save_settings(BotSettings(admin_chat_ids=bad_ids))  # type: ignore[arg-type]


def test_settings_rejects_non_loopback_host(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    for bad_host in ("0.0.0.0", "example.com", "192.168.1.1", "", "   ", None, 42):
        with pytest.raises(ValueError):
            config.save_settings(BotSettings(comfy_host=bad_host))  # type: ignore[arg-type]
    for good_host in ("127.0.0.1", "localhost", "::1"):
        saved = config.save_settings(BotSettings(comfy_host=good_host))
        assert saved.comfy_host == good_host


def test_settings_rejects_bad_port(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    for bad_port in (0, -1, 65536, "8188", 81.88, True, None):
        with pytest.raises(ValueError):
            config.save_settings(BotSettings(comfy_port=bad_port))  # type: ignore[arg-type]


def test_settings_rejects_bad_review_mode_and_wrong_types(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    with pytest.raises(ValueError):
        config.save_settings(BotSettings(review_mode="yes"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        config.save_settings("nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        config.save_settings(
            BotSettings(triggers=["t1"])  # type: ignore[list-item]
        )


def test_trigger_rejects_empty_fields() -> None:
    with pytest.raises(ValueError):
        Trigger(name="", prompt_file="p.json")
    with pytest.raises(ValueError):
        Trigger(name="   ", prompt_file="p.json")
    with pytest.raises(ValueError):
        Trigger(name="t", prompt_file="")
    with pytest.raises(ValueError):
        Trigger(name="t", prompt_file=42)  # type: ignore[arg-type]


def test_settings_rejects_duplicate_trigger_names(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    with pytest.raises(ValueError, match="dup"):
        config.save_settings(
            BotSettings(
                triggers=[
                    Trigger(name="dup", prompt_file="a.json"),
                    Trigger(name="dup", prompt_file="b.json"),
                ]
            )
        )


def test_settings_load_rejects_corrupt_object(tmp_path: Path) -> None:
    cases = [
        {"settings": "nope"},
        {"settings": {"comfy_host": "example.com"}},
        {"settings": {"comfy_port": 0}},
        {"settings": {"admin_chat_ids": [""]}},
        {"settings": {"review_mode": "yes"}},
        {"settings": {"bogus_key": 1}},
        {"settings": {"triggers": [{"name": "t"}]}},
        {
            "settings": {
                "triggers": [
                    {"name": "t", "prompt_file": "a.json"},
                    {"name": "t", "prompt_file": "b.json"},
                ]
            }
        },
    ]
    for payload in cases:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            ConfigStore(path)


def test_settings_file_round_trips_json_shape(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    config.save_settings(
        BotSettings(
            admin_chat_ids=["42"],
            triggers=[Trigger(name="t", prompt_file="p.json")],
        )
    )
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert raw["settings"]["admin_chat_ids"] == ["42"]
    assert raw["settings"]["triggers"] == [{"name": "t", "prompt_file": "p.json"}]
    assert TOKEN_A.encode() not in (tmp_path / "config.json").read_bytes()


# ---------------------------------------------------------------------------
# T080/T081 trigger prompt targets + publish block (append-only additions)
# ---------------------------------------------------------------------------


def test_prompt_target_rejects_empty_fields() -> None:
    with pytest.raises(ValueError):
        PromptTarget(node="")
    with pytest.raises(ValueError):
        PromptTarget(node="  ")
    with pytest.raises(ValueError):
        PromptTarget(node="28", input="")
    with pytest.raises(ValueError):
        PromptTarget(node=28)  # type: ignore[arg-type]
    assert PromptTarget(node="28") == PromptTarget(node="28", input="text")


def test_publish_spec_validation() -> None:
    PublishSpec(account="a", destination="d")  # defaults incl. {{prompt}}
    with pytest.raises(ValueError):
        PublishSpec(account="", destination="d")
    with pytest.raises(ValueError):
        PublishSpec(account="a", destination="")
    with pytest.raises(ValueError):
        PublishSpec(account="a", destination="d", format="gif")
    for bad_quality in (0, 101, True, "90", None):
        with pytest.raises(ValueError):
            PublishSpec(account="a", destination="d", quality=bad_quality)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PublishSpec(account="a", destination="d", source="")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PublishSpec(account="a", destination="d", caption_template=None)  # type: ignore[arg-type]


def test_trigger_new_fields_default_and_validate() -> None:
    trigger = Trigger(name="t", prompt_file="p.json")
    assert trigger.prompt_targets == ()
    assert trigger.prompt_required is False
    assert trigger.publish is None
    with pytest.raises(ValueError):
        Trigger(name="t", prompt_file="p.json",
                prompt_targets=["28"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        Trigger(name="t", prompt_file="p.json",
                prompt_required="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Trigger(name="t", prompt_file="p.json",
                publish={"account": "a"})  # type: ignore[arg-type]


def test_settings_trigger_new_fields_round_trip(tmp_path: Path) -> None:
    config, _ = make_store(tmp_path)
    saved = config.save_settings(
        BotSettings(
            admin_chat_ids=["1"],
            triggers=[
                Trigger(
                    name="anima",
                    prompt_file="anima.json",
                    prompt_targets=[PromptTarget(node="28")],
                    prompt_required=True,
                    publish=PublishSpec(
                        account="a", destination="d", source="6:0",
                        caption_template="{{prompt}}", format="jpeg",
                        quality=80,
                    ),
                )
            ],
        )
    )
    trigger = saved.triggers[0]
    assert trigger.prompt_targets == (PromptTarget(node="28"),)
    assert trigger.prompt_required is True
    assert trigger.publish == PublishSpec(
        account="a", destination="d", source="6:0",
        caption_template="{{prompt}}", format="jpeg", quality=80,
    )
    reloaded = ConfigStore(tmp_path / "config.json")
    assert reloaded.get_settings() == saved
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert raw["settings"]["triggers"][0] == {
        "name": "anima",
        "prompt_file": "anima.json",
        "prompt_targets": [{"node": "28", "input": "text"}],
        "prompt_required": True,
        "publish": {
            "account": "a", "destination": "d", "source": "6:0",
            "format": "jpeg", "quality": 80,
        },
    }


def test_settings_trigger_old_files_without_new_keys_load(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"settings": {"triggers": [{"name": "t",
                                        "prompt_file": "p.json"}]}}
        ),
        encoding="utf-8",
    )
    trigger = ConfigStore(path).get_settings().triggers[0]
    assert trigger.prompt_targets == ()
    assert trigger.prompt_required is False
    assert trigger.publish is None


def test_settings_trigger_rejects_bad_new_fields(tmp_path: Path) -> None:
    bad_triggers = [
        {"name": "t", "prompt_file": "p.json", "prompt_targets": "28"},
        {"name": "t", "prompt_file": "p.json",
         "prompt_targets": [{"node": ""}]},
        {"name": "t", "prompt_file": "p.json",
         "prompt_targets": [{"node": "28", "bogus": 1}]},
        {"name": "t", "prompt_file": "p.json", "prompt_required": "yes"},
        {"name": "t", "prompt_file": "p.json",
         "publish": {"account": "a"}},
        {"name": "t", "prompt_file": "p.json",
         "publish": {"account": "a", "destination": "d", "bogus": 1}},
        {"name": "t", "prompt_file": "p.json",
         "publish": {"account": "a", "destination": "d", "format": "gif"}},
        {"name": "t", "prompt_file": "p.json", "bogus_key": 1},
    ]
    for bad_trigger in bad_triggers:
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"settings": {"triggers": [bad_trigger]}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            ConfigStore(path)
