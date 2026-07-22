from __future__ import annotations

import json

import httpx
import pytest

from qq_mcp_server.onebot import OneBotClient, OneBotError


@pytest.mark.asyncio
async def test_history_request_is_read_only_and_disables_media_resolution() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/get_group_msg_history"
        assert request.headers["authorization"] == "Bearer token"
        assert json.loads(request.content) == {
            "group_id": "2",
            "count": 100,
            "reverse_order": True,
            "disable_get_url": True,
            "parse_mult_msg": False,
            "quick_reply": False,
            "message_seq": "99",
        }
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"messages": []}})

    client = OneBotClient("http://127.0.0.1:3000", "token", transport=httpx.MockTransport(handler))
    assert await client.get_group_history("2", 100, message_seq="99") == []
    await client.close()


@pytest.mark.asyncio
async def test_private_action_guard_rejects_send() -> None:
    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    with pytest.raises(OneBotError, match="非只读"):
        await client._action("send_group_msg")
    await client.close()
