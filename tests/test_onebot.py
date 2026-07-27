from __future__ import annotations

import json

import httpx
import pytest

from qq_mcp_server.onebot import (
    OneBotClient,
    OneBotError,
    OneBotSessionError,
    onebot_action_source,
)


@pytest.mark.asyncio
async def test_status_reads_local_online_flag_without_other_actions() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert json.loads(request.content) == {}
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": {"online": False, "good": True, "stat": {}},
            },
        )

    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(handler),
    )
    assert await client.get_status() == {"online": False, "good": True, "stat": {}}
    assert calls == ["/get_status"]
    await client.close()


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


@pytest.mark.asyncio
async def test_group_registry_actions_are_normalized_and_read_only() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/get_group_list":
            assert json.loads(request.content) == {"no_cache": True}
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": [
                        {"group_id": 2, "group_name": "测试群", "member_count": 4},
                        {"group_id": "bad", "group_name": "忽略"},
                    ],
                },
            )
        assert request.url.path == "/get_group_member_list"
        assert json.loads(request.content) == {"group_id": "2", "no_cache": False}
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": [
                    {
                        "group_id": 2,
                        "user_id": 10,
                        "card": "角色名",
                        "nickname": "昵称",
                        "role": "member",
                    }
                ],
            },
        )

    client = OneBotClient("http://127.0.0.1:3000", "token", transport=httpx.MockTransport(handler))
    assert await client.get_group_list() == [
        {
            "group_id": "2",
            "group_name": "测试群",
            "member_count": 4,
            "max_member_count": 0,
        }
    ]
    assert await client.get_group_member_list("2") == [
        {
            "qq_user_id": "10",
            "display_name": "角色名",
            "card": "角色名",
            "nickname": "昵称",
            "onebot_role": "member",
        }
    ]
    await client.close()


@pytest.mark.asyncio
async def test_explicit_login_failure_is_classified_and_counted() -> None:
    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "status": "failed",
                    "retcode": 1404,
                    "message": "当前未登录，登录状态已失效",
                },
            )
        ),
    )

    with pytest.raises(OneBotSessionError, match="未登录"):
        await client.get_login_info()

    stats = client.stats()["get_login_info"]
    assert stats["calls"] == 1
    assert stats["errors"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_onebot_action_audit_records_explicit_source_and_outcome() -> None:
    records: list[dict[str, object]] = []
    client = OneBotClient(
        "http://127.0.0.1:3000",
        "token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": "1"},
                },
            )
        ),
        audit_hook=records.append,
    )

    with onebot_action_source(client, "account_switch_finalize"):
        await client.get_login_info()

    assert records[0]["action"] == "get_login_info"
    assert records[0]["source"] == "account_switch_finalize"
    assert records[0]["outcome"] == "success"
    await client.close()
