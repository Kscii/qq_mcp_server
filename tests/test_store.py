from __future__ import annotations

import sqlite3

import pytest

from qq_mcp_server.models import CardOperation, ChatMessage, NoteOperation
from qq_mcp_server.store import MessageStore, VersionConflictError


def test_old_database_is_rejected_instead_of_partially_migrated(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="不执行迁移"):
        MessageStore(path)


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
