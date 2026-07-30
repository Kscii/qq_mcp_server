from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from qq_mcp_server.config import AppConfig
from qq_mcp_server.runtime import NapCatRuntime, sync_freshness
from qq_mcp_server.store import MessageStore


class RuntimeClient:
    def __init__(
        self,
        *,
        listed: bool = False,
        online: bool = True,
        history: dict[str | None, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.listed = listed
        self.online = online
        self.history = history or {}
        self.actions: list[str] = []
        self.history_cursors: list[str | None] = []

    async def get_login_info(self) -> dict[str, Any]:
        self.actions.append("get_login_info")
        return {"user_id": "1", "nickname": "测试账号"}

    async def get_status(self) -> dict[str, Any]:
        self.actions.append("get_status")
        return {"online": self.online, "good": self.online}

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
        self.actions.append("get_group_history")
        self.history_cursors.append(message_seq)
        return self.history.get(message_seq, [])[:count]


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
    assert control["source"] == "event_transport"


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


async def test_watchdog_does_not_invent_gap_when_napcat_never_sent_heartbeat(
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MessageStore(config.database_path)
    runtime = NapCatRuntime(
        config,
        RuntimeClient(),  # type: ignore[arg-type]
        store,
        "token",
    )
    store.upsert_group_candidate("2", "测试群", source="group_message_event")
    store.set_runtime_status(
        "sse",
        {
            "connected": True,
            "last_event_at": datetime.now(UTC).isoformat(),
            "last_heartbeat_at": None,
            "heartbeat_interval_ms": None,
        },
    )

    async def stop_after_first_check(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("qq_mcp_server.runtime.asyncio.sleep", stop_after_first_check)
    with pytest.raises(asyncio.CancelledError):
        await runtime.run_watchdog_forever()

    watchdog = store.runtime_status("collector_watchdog")
    assert watchdog["heartbeat_observed"] is False
    assert watchdog["heartbeat_stale"] is False
    assert store.list_message_gaps(group_id="2", unresolved_only=True) == []


async def test_local_offline_status_pauses_once_and_opens_one_gap(config: AppConfig) -> None:
    store = MessageStore(config.database_path)
    store.upsert_group_candidate("2", "测试群", source="group_message_event")
    client = RuntimeClient(online=False)
    runtime = NapCatRuntime(
        config,
        client,  # type: ignore[arg-type]
        store,
        "token",
    )
    now = datetime.now(UTC)

    await runtime._check_session_status(now)
    await runtime._check_session_status(now + timedelta(minutes=1))

    control = store.runtime_status("collection_control")
    health = store.runtime_status("session_health")
    gaps = store.list_message_gaps(group_id="2", unresolved_only=True)
    assert control["status"] == "paused_session"
    assert health["recovery_state"] == "offline"
    assert health["qq_online"] is False
    assert len(gaps) == 1
    assert client.actions == ["get_status", "get_status"]


async def test_recovery_waits_five_minutes_then_verifies_once(config: AppConfig) -> None:
    store = MessageStore(config.database_path)
    store.upsert_group_candidate("2", "测试群", source="group_message_event")
    client = RuntimeClient(online=True)
    runtime = NapCatRuntime(
        config,
        client,  # type: ignore[arg-type]
        store,
        "token",
    )
    runtime.manager.pause_session(RuntimeError("掉线"), source="test")
    started = datetime.now(UTC)
    store.set_runtime_status(
        "session_health",
        {
            "qq_online": True,
            "onebot_reachable": True,
            "offline_since": (started - timedelta(minutes=1)).isoformat(),
            "online_since": started.isoformat(),
            "consecutive_online_checks": 1,
            "recovery_state": "stabilizing",
        },
    )

    await runtime._check_session_status(started + timedelta(minutes=4, seconds=59))
    assert store.runtime_status("collection_control")["status"] == "paused_session"
    assert client.actions == ["get_status"]

    await runtime._check_session_status(started + timedelta(minutes=5))
    assert store.runtime_status("collection_control")["status"] == "active"
    assert store.runtime_status("session_health")["recovery_state"] == "active"
    assert client.actions == ["get_status", "get_status", "get_login_info"]


async def test_explicit_self_kick_archives_but_ambiguous_event_does_not(
    config: AppConfig,
) -> None:
    store = MessageStore(config.database_path)
    explicit_group = store.whitelist_group("2", "明确退出群")
    ambiguous_group = store.whitelist_group("3", "不确定群")
    runtime = NapCatRuntime(
        config,
        RuntimeClient(),  # type: ignore[arg-type]
        store,
        "token",
    )

    await runtime.handle_event(
        {
            "time": 10,
            "self_id": "1",
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "kick_me",
            "group_id": "2",
            "user_id": "1",
        }
    )
    await runtime.handle_event(
        {
            "time": 11,
            "self_id": "1",
            "post_type": "notice",
            "notice_type": "group_decrease",
            "group_id": "3",
            "user_id": "1",
        }
    )

    explicit = store.get_group(str(explicit_group["group_key"]))
    ambiguous = store.get_group(str(ambiguous_group["group_key"]))
    assert explicit["archived"] is True
    assert explicit["membership_status"] == "kicked"
    assert ambiguous["archived"] is False
    assert ambiguous["membership_status"] == "uncertain"


async def test_automatic_recovery_uses_current_session_cursor_and_repairs_gap(
    config: AppConfig,
) -> None:
    before = group_event("100")
    before["time"] = 100
    after = group_event("200")
    after["time"] = 200
    missing = group_event("150")
    missing["time"] = 150
    client = RuntimeClient(history={None: [after, missing, before]})
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    runtime = NapCatRuntime(
        config,
        client,  # type: ignore[arg-type]
        store,
        "token",
    )
    await runtime.handle_event(before)
    await runtime.handle_event(after)
    gap = store.create_message_gap(
        "2",
        start_at=110,
        end_at=190,
        confidence="confirmed",
        source="session_offline",
    )
    store.set_runtime_status(
        "session_health",
        {
            "qq_online": True,
            "recovery_state": "active",
            "session_generation": 2,
        },
    )

    result = await runtime._automatic_history_recovery_tick()

    assert result["status"] == "complete"
    assert client.history_cursors == [None]
    assert store.message_gap(str(gap["gap_id"]))["status"] == "repaired"
    assert store.state("2")["message_count"] == 3
    jobs = store.list_recovery_jobs(group_id="2")
    assert jobs[0]["session_generation"] == 2
    assert jobs[0]["cursor"] is None
    assert group["archived"] is False


async def test_verified_relogin_discards_all_legacy_history_cursors(
    config: AppConfig,
) -> None:
    store = MessageStore(config.database_path)
    store.whitelist_group("2", "测试群")
    gap = store.create_message_gap(
        "2",
        start_at=100,
        end_at=200,
        confidence="confirmed",
        source="session_offline",
    )
    store.update_message_gap_repair(
        str(gap["gap_id"]),
        status="paused",
        repair_cursor="348582019",
    )
    store.update_state(
        account_id="1",
        group_id="2",
        reconcile_cursor="348582019",
        reconcile_boundary_id="old-boundary",
        reconcile_newest_id="old-newest",
    )
    runtime = NapCatRuntime(
        config,
        RuntimeClient(),  # type: ignore[arg-type]
        store,
        "token",
    )

    result = await runtime._automatic_history_recovery(
        [gap],
        recovery_key="new-login",
    )

    state = store.state("2")
    refreshed_gap = store.message_gap(str(gap["gap_id"]))
    assert result["status"] == "queued"
    assert state["reconcile_cursor"] is None
    assert state["reconcile_boundary_id"] is None
    assert state["reconcile_newest_id"] is None
    assert refreshed_gap["repair_cursor"] is None
    assert "旧分页游标已丢弃" in refreshed_gap["last_error"]


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
