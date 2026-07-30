from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from qq_mcp_server.models import (
    ROLEPLAY_GUIDANCE_MAX_LENGTH,
    CardOperation,
    ChatMessage,
    NoteOperation,
)
from qq_mcp_server.store import MessageStore, VersionConflictError, backup_database


def test_old_database_is_rejected_instead_of_partially_migrated(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="不执行迁移"):
        MessageStore(path)


def test_schema_v2_is_atomically_migrated_to_v5(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "v2.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO app_metadata VALUES ('schema_version', '2');
        CREATE TABLE groups (
            group_key TEXT PRIMARY KEY,
            qq_group_id TEXT NOT NULL UNIQUE,
            qq_group_name TEXT NOT NULL,
            module_title TEXT NOT NULL DEFAULT '',
            display_label TEXT NOT NULL DEFAULT '',
            roleplay_guidance TEXT NOT NULL DEFAULT '',
            roleplay_enabled INTEGER NOT NULL DEFAULT 0,
            whitelisted INTEGER NOT NULL DEFAULT 1,
            version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE sync_state (
            group_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            latest_message_id TEXT,
            oldest_message_seq TEXT,
            recent_ready INTEGER NOT NULL DEFAULT 0,
            initial_import_complete INTEGER NOT NULL DEFAULT 0,
            last_sync_at TEXT,
            last_error TEXT
        );
        """
    )
    connection.commit()
    connection.close()

    MessageStore(path)

    migrated = sqlite3.connect(path)
    assert migrated.execute(
        "SELECT value FROM app_metadata WHERE key = 'schema_version'"
    ).fetchone() == ("5",)
    group_columns = {row[1] for row in migrated.execute("PRAGMA table_info(groups)")}
    sync_columns = {row[1] for row in migrated.execute("PRAGMA table_info(sync_state)")}
    assert "history_since" in group_columns
    assert {
        "archived",
        "archived_at",
        "archive_reason",
        "archive_source",
        "membership_status",
        "membership_updated_at",
    } <= group_columns
    assert {
        "reconcile_cursor",
        "reconcile_boundary_id",
        "reconcile_newest_id",
    } <= sync_columns
    tables = {
        row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "collector_sessions",
        "message_gaps",
        "onebot_action_audit",
        "qq_accounts",
        "qq_account_switches",
        "history_request_slots",
        "recovery_jobs",
    } <= tables
    migrated.close()


def test_v4_to_v5_migration_archives_groups_by_roleplay_switch(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    active = store.whitelist_group("2", "当前团")
    inactive = store.whitelist_group("3", "旧团")
    store.set_group_enabled(str(active["group_key"]), expected_version=0, enabled=True)
    connection = sqlite3.connect(config.database_path)
    connection.execute("UPDATE app_metadata SET value = '4' WHERE key = 'schema_version'")
    connection.execute(
        """UPDATE groups SET archived = 0, archived_at = NULL,
                  archive_reason = NULL, archive_source = NULL"""
    )
    connection.commit()
    connection.close()

    migrated = MessageStore(config.database_path)

    assert migrated.get_group(str(active["group_key"]))["archived"] is False
    migrated_inactive = migrated.get_group(str(inactive["group_key"]))
    assert migrated_inactive["archived"] is True
    assert migrated_inactive["archive_source"] == "migration"


def test_online_backup_is_integrity_checked_and_private(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    store.whitelist_group("2", "测试群")

    backup = backup_database(config.database_path, config.database_path.parent / "backups")

    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    connection = sqlite3.connect(backup)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("SELECT count(*) FROM groups").fetchone() == (1,)
    connection.close()


def message(message_id: str, *, group_id: str = "2") -> ChatMessage:
    return ChatMessage(
        group_id=group_id,
        message_id=message_id,
        message_seq=message_id,
        sent_at=int(message_id),
        sender_id="10",
        sender_nickname="玩家",
        sender_card="角色",
        sender_display="角色",
        plain_text=f"消息-{message_id}",
        reply_to_message_id=None,
        contains_unsupported_media=False,
    )


def minimal_card(name: str = "调查员") -> dict[str, object]:
    return {
        "schema_version": 1,
        "template_id": "test",
        "character_id": "investigator",
        "identity": {"name": name, "player": "玩家"},
        "attributes": {"str": 50, "luck": 60},
        "vitals": {"hp": {"current": 10, "max": 10}},
        "skills": {"侦查": {"name": "侦查", "regular": 50, "hard": 25, "extreme": 10}},
        "inventory": [],
        "experiences": [],
        "myth_contacts": [],
        "weapons": [],
        "assets": {},
        "background": {},
        "era_time": {},
        "provenance": {},
    }


def ready_store(config) -> tuple[MessageStore, dict[str, object]]:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    store.replace_character(
        str(group["group_key"]),
        source_filename="card.xlsx",
        source_sha256="a" * 64,
        source_path="/private/card.xlsx",
        base_card=minimal_card(),
        current_card=minimal_card(),
        clear_runtime_data=True,
    )
    return store, store.get_group(str(group["group_key"]))


def test_whitelist_removal_revokes_access_but_preserves_messages(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    store.upsert([message("1")])
    store.remove_from_whitelist(str(group["group_key"]))
    assert store.state("2")["message_count"] == 1
    assert store.list_groups() == []
    restored = store.whitelist_group("2", "测试群新名")
    assert restored["group_key"] == group["group_key"]


def test_archive_is_reversible_read_only_state(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    archived = store.set_group_archived(
        str(group["group_key"]),
        archived=True,
        reason="结团",
        source="manual",
    )

    assert archived["archived"] is True
    assert archived["roleplay_enabled"] is False
    assert archived["whitelisted"] is True
    assert archived["archive_source"] == "manual"
    with pytest.raises(ValueError, match="GROUP_ARCHIVED"):
        store.update_group_profile(
            str(group["group_key"]),
            expected_version=int(archived["version"]),
            module_title="不能修改",
            display_label=None,
            roleplay_guidance=None,
        )

    restored = store.set_group_archived(
        str(group["group_key"]),
        archived=False,
        reason="重新开团",
        source="manual",
    )
    assert restored["archived"] is False
    assert restored["roleplay_enabled"] is False


def test_membership_event_only_reopens_automatic_archive(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    store.update_group_membership(
        "2",
        status="kicked",
        reason="被踢",
        automatic_archive=True,
    )
    assert store.get_group(str(group["group_key"]))["archived"] is True

    store.update_group_membership(
        "2",
        status="joined",
        reason="重新入群",
        automatic_archive=False,
    )
    restored = store.get_group(str(group["group_key"]))
    assert restored["archived"] is False
    assert restored["membership_status"] == "joined"

    store.set_group_archived(
        str(group["group_key"]),
        archived=True,
        reason="手动结团",
        source="manual",
    )
    store.update_group_membership(
        "2",
        status="joined",
        reason="仍在群里",
        automatic_archive=False,
    )
    assert store.get_group(str(group["group_key"]))["archived"] is True


def test_history_request_budget_is_atomic_and_rolling(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    started = datetime.now(UTC) + timedelta(seconds=1)

    first = store.claim_history_request("test", now=started)
    cooling = store.claim_history_request("test", now=started + timedelta(seconds=59))
    second = store.claim_history_request("test", now=started + timedelta(seconds=60))

    assert first["allowed"] is True
    assert cooling["allowed"] is False
    assert cooling["reason"] == "minimum_interval"
    assert second["allowed"] is True
    assert store.history_request_budget(now=started + timedelta(seconds=60))["used"] == 2


def test_atomic_card_and_note_commit_then_whole_undo(config) -> None:  # type: ignore[no-untyped-def]
    store, group = ready_store(config)
    store.upsert([message("1")])
    result = store.commit_turn_updates(
        str(group["group_key"]),
        expected_version=int(group["version"]),
        origin="qq_event",
        card_operations=[
            CardOperation(
                op="increment",
                path="/vitals/hp/current",
                value=-2,
                source_message_ids=["1"],
                reason="骰娘明确结算 HP-2",
            )
        ],
        note_operations=[
            NoteOperation(
                op="create",
                category="clue",
                title="旧宅钥匙",
                content="在桌下发现一把旧钥匙。",
                source_message_ids=["1"],
            )
        ],
        summary="HP-2 并记录钥匙线索",
    )
    assert store.character(str(group["group_key"]))["current"]["vitals"]["hp"]["current"] == 8  # type: ignore[index]
    assert len(store.notes(str(group["group_key"]))) == 1

    version = store.get_group(str(group["group_key"]))["version"]
    undo = store.undo_change(
        str(group["group_key"]),
        change_id=str(result["change_id"]),
        expected_version=int(version),
        reason="AI 误读骰点",
    )
    assert undo["undid_change_id"] == result["change_id"]
    assert store.character(str(group["group_key"]))["current"]["vitals"]["hp"]["current"] == 10  # type: ignore[index]
    assert store.notes(str(group["group_key"])) == []


def test_version_conflict_prevents_overwrite(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    store.update_group_profile(
        str(group["group_key"]),
        expected_version=0,
        module_title="模组",
        display_label=None,
        roleplay_guidance=None,
    )
    try:
        store.update_group_profile(
            str(group["group_key"]),
            expected_version=0,
            module_title="旧版本覆盖",
            display_label=None,
            roleplay_guidance=None,
        )
    except VersionConflictError as error:
        assert error.current_version == 1
    else:
        raise AssertionError("expected VersionConflictError")


def test_history_since_is_per_group_and_does_not_delete_messages(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    store.upsert([message("1")])
    store.update_state(
        account_id="1",
        group_id="2",
        recent_ready=True,
        initial_import_complete=True,
    )

    updated = store.update_group_profile(
        str(group["group_key"]),
        expected_version=0,
        module_title=None,
        display_label=None,
        roleplay_guidance=None,
        history_since="2026-07-01T00:00:00+08:00",
    )

    assert updated["history_since"] == "2026-07-01T00:00:00+08:00"
    assert store.state("2")["initial_import_complete"] is False
    assert store.state("2")["message_count"] == 1


def test_roleplay_guidance_accepts_configured_limit_and_rejects_more(config) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    accepted = "界" * ROLEPLAY_GUIDANCE_MAX_LENGTH
    updated = store.update_group_profile(
        str(group["group_key"]),
        expected_version=0,
        module_title=None,
        display_label=None,
        roleplay_guidance=f"\n{accepted}\n",
    )
    assert updated["roleplay_guidance"] == accepted

    with pytest.raises(
        ValueError,
        match=(
            f"当前为 {ROLEPLAY_GUIDANCE_MAX_LENGTH + 1} 字，"
            f"不能超过 {ROLEPLAY_GUIDANCE_MAX_LENGTH} 字"
        ),
    ):
        store.update_group_profile(
            str(group["group_key"]),
            expected_version=1,
            module_title=None,
            display_label=None,
            roleplay_guidance="界" * (ROLEPLAY_GUIDANCE_MAX_LENGTH + 1),
        )

    cleared = store.update_group_profile(
        str(group["group_key"]),
        expected_version=1,
        module_title=None,
        display_label=None,
        roleplay_guidance="  ",
    )
    assert cleared["roleplay_guidance"] == ""


def test_registered_qq_accounts_share_player_identity_without_copying_group_data(
    config,
) -> None:  # type: ignore[no-untyped-def]
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    group_key = str(group["group_key"])
    store.ensure_active_qq_account("1")
    store.set_member_roles(
        group_key,
        expected_version=0,
        player_qq_user_id="1",
        kp_qq_user_ids=[],
        dice_bot_qq_user_ids=[],
    )

    store.register_qq_account("9", label="备用账号")

    assert store.member_roles(group_key)["player_qq_user_ids"] == ["1", "9"]
    assert len(store.list_groups()) == 1
    assert store.active_qq_account()["account_id"] == "1"  # type: ignore[index]
