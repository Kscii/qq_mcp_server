from __future__ import annotations

from fastmcp import Client

from qq_mcp_server.config import AppConfig
from qq_mcp_server.mcp_server import create_mcp
from qq_mcp_server.models import ChatMessage
from qq_mcp_server.store import MessageStore


def message(message_id: str, text: str) -> ChatMessage:
    return ChatMessage(
        group_id="2",
        message_id=message_id,
        message_seq=message_id,
        sent_at=int(message_id),
        sender_id="10",
        sender_nickname="玩家",
        sender_card="角色",
        sender_display="角色",
        plain_text=text,
        reply_to_message_id=None,
        contains_unsupported_media=False,
    )


async def test_mcp_exposes_only_three_read_only_tools(config: AppConfig) -> None:
    store = MessageStore(config.database_path)
    store.upsert([message("1", "第一条"), message("2", "第二条")])
    mcp = create_mcp(config, store)
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "get_recent_messages",
        "search_messages",
        "get_sync_status",
    }
    assert all(tool.annotations.readOnlyHint for tool in tools if tool.annotations)

    async with Client(mcp) as client:
        result = await client.call_tool("get_recent_messages", {"limit": 1})
    assert result.data["messages"][0]["plain_text"] == "第二条"
    assert "未经信任" in result.data["notice"]
