from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from qq_mcp_server.config import AppConfig
from qq_mcp_server.runtime import NapCatRuntime, sync_freshness
from qq_mcp_server.store import MessageStore


class RuntimeClient:
    def __init__(self, *, listed: bool = False) -> None:
        self.listed = listed

    async def get_login_info(self) -> dict[str, Any]:
        return {"user_id": "1", "nickname": "测试账号"}

    async def get_group_list(self) -> list[dict[str, Any]]:
        if not self.listed:
            return []
        return [
            {
                "group_id": "2",
                "group_name": "测试群",
                "member_count": 2,
                "max_member_count": 200,
            }
        ]

    async def get_group_info(self, group_id: str, *, no_cache: bool = False) -> dict[str, Any]:
        assert no_cache is True
        return {"group_id": group_id, "group_name": "测试群"}

    async def get_group_member_list(
        self, group_id: str, *, no_cache: bool = False
    ) -> list[dict[str, Any]]:
        assert no_cache is True
        return [{"qq_user_id": "1", "display_name": "测试账号"}]

    async def get_group_history(
        self, group_id: str, count: int, *, message_seq: str | None = None
    ) -> list[dict[str, Any]]:
        return []


def group_event(message_id: str = "10", *, group_id: str = "2") -> dict[str, Any]:
    return {
        "time": 10,
        "self_id": "1",
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": "9",
        "message_id": message_id,
        "message_seq": message_id,
        "sender": {"nickname": "玩家", "card": "角色"},
        "message": [{"type": "text", "data": {"text": "新消息"}}],
    }


async def test_sse_event_discovers_and_stores_every_group_without_granting_ai_access(
    config: AppConfig,
) -> None:
    store = MessageStore(config.database_path)
    runtime = NapCatRuntime(
        config,
        RuntimeClient(),  # type: ignore[arg-type]
        store,
        "token",
    )

    await runtime.handle_event(group_event(group_id="3"))

    candidate = store.group_candidate("3")
    assert candidate is not None
    assert candidate["source"] == "group_message_event"
    assert store.state("3")["message_count"] == 1
    assert store.get_group_by_qq("3") is None

    store.whitelist_group("2", "测试群")
    await runtime.handle_event(group_event())
    await runtime.handle_event(group_event())
    assert store.state("2")["message_count"] == 1


async def test_direct_probe_verifies_membership_when_registry_is_stale(
    config: AppConfig,
) -> None:
    store = MessageStore(config.database_path)
    runtime = NapCatRuntime(
        config,
        RuntimeClient(listed=False),  # type: ignore[arg-type]
        store,
        "token",
    )

    result = await runtime.probe_group("2")

    assert result["status"] == "group_registry_stale"
    assert result["verification_method"] == "member_list"
    candidate = store.group_candidate("2")
    assert candidate is not None
    assert candidate["verification_valid"] is True


async def test_sse_account_mismatch_persistently_pauses_collection(
    config: AppConfig,
) -> None:
    store = MessageStore(config.database_path)
    runtime = NapCatRuntime(
        config,
        RuntimeClient(),  # type: ignore[arg-type]
        store,
        "token",
    )
    event = group_event()
    event["self_id"] = "999"

    await runtime.handle_event(event)

    assert runtime.manager.is_active() is False
    control = store.runtime_status("collection_control")
    assert control["status"] == "paused_session"
    assert control["source"] == "sse_event"


async def test_group_registry_error_never_authorizes_napcat_restart(
    config: AppConfig,
) -> None:
    store = MessageStore(config.database_path)
    runtime = NapCatRuntime(
        config,
        RuntimeClient(),  # type: ignore[arg-type]
        store,
        "token",
    )
    store.set_runtime_status(
        "group_registry",
        {
            "ok": False,
            "last_error": "OneBotError: 群列表缓存异常",
            "group_ids": [],
        },
    )
    store.set_runtime_status("sse", {"connected": True, "online": True, "good": True})

    status = await runtime.get_status()

    assert status["status"] == "healthy"
    assert status["onebot_reachable"] is True
    assert all("恢复 NapCat" not in action["label"] for action in status["next_actions"])


def test_sync_error_does_not_make_stale_state_look_fresh(config: AppConfig) -> None:
    store = MessageStore(config.database_path)
    store.update_state(
        account_id="1",
        group_id="2",
        recent_ready=True,
        error=None,
    )
    successful_at = store.state("2")["last_sync_at"]
    store.record_error(account_id="1", group_id="2", error="读取失败")
    state = store.state("2")

    assert state["last_sync_at"] == successful_at
    assert sync_freshness(state, 60)["fresh"] is False
    assert datetime.fromisoformat(str(successful_at)).tzinfo == UTC


def test_context_freshness_boundary_is_sixty_seconds() -> None:
    now = datetime.now(UTC)
    fresh = {
        "last_sync_at": (now - timedelta(seconds=59)).isoformat(),
        "last_error": None,
    }
    stale = {
        "last_sync_at": (now - timedelta(seconds=61)).isoformat(),
        "last_error": None,
    }
    assert sync_freshness(fresh, 60)["fresh"] is True
    assert sync_freshness(stale, 60)["fresh"] is False
