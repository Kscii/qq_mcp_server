from __future__ import annotations

from pathlib import Path

from qq_mcp_server.exporter import TextExporter
from qq_mcp_server.models import ChatMessage
from qq_mcp_server.store import MessageStore


def message(message_id: str, sent_at: int, text: str = "内容") -> ChatMessage:
    return ChatMessage(
        group_id="2",
        message_id=message_id,
        message_seq=message_id,
        sent_at=sent_at,
        sender_id=f"qq-{message_id}",
        sender_nickname="昵称",
        sender_card="角色",
        sender_display="角色",
        plain_text=text,
        reply_to_message_id=None,
        contains_unsupported_media=False,
    )


def test_upsert_query_and_text_export_are_deterministic(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.sqlite3")
    assert store.upsert([message("2", 200), message("1", 100, "最早")]) == (2, 2)
    assert store.upsert([message("1", 100, "已更新")]) == (1, 0)
    assert [item["message_id"] for item in store.recent("2", limit=10)] == ["1", "2"]
    assert [
        item["message_id"]
        for item in store.search(
            "2", query="更新", sender_id=None, start_timestamp=None, end_timestamp=None, limit=10
        )
    ] == ["1"]

    path = tmp_path / "group.txt"
    TextExporter(
        store,
        group_id="2",
        group_name="测试群",
        path=path,
        timezone="Asia/Shanghai",
    ).write()
    exported = path.read_text(encoding="utf-8")
    assert "# QQ 群文字归档：测试群" in exported
    assert "角色（QQ qq-1）" in exported
    assert exported.index("已更新") < exported.index("内容")
    assert not (tmp_path / ".group.txt.tmp").exists()


def test_recent_paginates_before_stable_message_id(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.sqlite3")
    store.upsert([message(str(index), index) for index in range(1, 6)])
    page = store.recent("2", limit=2, before_message_id="4")
    assert [item["message_id"] for item in page] == ["2", "3"]
