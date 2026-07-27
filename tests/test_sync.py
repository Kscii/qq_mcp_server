from __future__ import annotations

from typing import Any

import pytest

from qq_mcp_server.config import AppConfig
from qq_mcp_server.models import GroupTarget
from qq_mcp_server.store import MessageStore
from qq_mcp_server.sync import (
    AccountMismatchError,
    CollectionPausedError,
    MultiGroupSyncManager,
    SyncService,
)


def raw(message_id: int, *, group_id: int = 2) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "message_id": message_id,
        "message_seq": message_id,
        "time": message_id,
        "user_id": 10,
        "sender": {"nickname": "玩家"},
        "message": [{"type": "text", "data": {"text": f"消息-{message_id}"}}],
    }


class FakeClient:
    def __init__(
        self,
        pages: dict[str | None, list[dict[str, Any]]],
        *,
        account_id: str = "1",
        fail_cursor: str | None = "never",
    ) -> None:
        self.pages = pages
        self.account_id = account_id
        self.fail_cursor = fail_cursor
        self.calls: list[str | None] = []
        self.login_calls = 0

    async def get_login_info(self) -> dict[str, Any]:
        self.login_calls += 1
        return {"user_id": self.account_id}

    async def get_group_info(self, group_id: str) -> dict[str, Any]:
        return {"group_id": group_id, "group_name": "测试群"}

    async def get_group_history(
        self, group_id: str, count: int, *, message_seq: str | None = None
    ) -> list[dict[str, Any]]:
        assert group_id == "2"
        self.calls.append(message_seq)
        if message_seq == self.fail_cursor:
            raise RuntimeError("临时失败")
        return self.pages.get(message_seq, [])[:count]


def service(
    config: AppConfig,
    client: FakeClient,
    *,
    history_since: str | None = "1970-01-01T00:00:00+00:00",
) -> SyncService:
    store = MessageStore(config.database_path)
    group = store.whitelist_group("2", "测试群")
    target = GroupTarget(str(group["group_key"]), "2", "测试群", history_since)
    return SyncService(config, target, client, store)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_recent_first_then_full_history_backfill(config: AppConfig) -> None:
    client = FakeClient(
        {None: [raw(5), raw(4), raw(3)], "3": [raw(3), raw(2), raw(1)], "1": [raw(1)]}
    )
    sync = service(config, client)
    recent = await sync.sync_recent()
    assert recent.inserted == 3
    assert sync.store.state("2")["recent_ready"] is True
    assert sync.store.state("2")["initial_import_complete"] is False

    result = await sync.import_all()
    assert result.inserted == 2
    assert result.complete is True
    assert sync.store.state("2")["message_count"] == 5
    assert sync.store.state("2")["initial_import_complete"] is True


@pytest.mark.asyncio
async def test_new_group_without_history_since_only_reads_recent_page(
    config: AppConfig,
) -> None:
    client = FakeClient({None: [raw(5), raw(4), raw(3)], "3": [raw(2), raw(1)]})
    sync = service(config, client, history_since=None)

    result = await sync.import_all()

    assert result.pages == 1
    assert client.calls == [None]
    assert sync.store.state("2")["initial_import_complete"] is True


@pytest.mark.asyncio
async def test_import_resumes_from_persisted_oldest_cursor(config: AppConfig) -> None:
    interrupted = FakeClient({None: [raw(5), raw(4), raw(3)]}, fail_cursor="3")
    sync = service(config, interrupted)
    with pytest.raises(RuntimeError, match="临时失败"):
        await sync.import_all()
    assert sync.store.state("2")["oldest_message_seq"] == "3"

    resumed = FakeClient({"3": [raw(3), raw(2), raw(1)], "1": [raw(1)]})
    resumed_sync = service(config, resumed)
    result = await resumed_sync.import_all()
    assert result.complete is True
    assert resumed.calls == [None, "3", "1"]
    assert resumed_sync.store.state("2")["message_count"] == 5


@pytest.mark.asyncio
async def test_recent_sync_walks_back_until_known_boundary(config: AppConfig) -> None:
    initial = service(config, FakeClient({None: [raw(3)], "3": [raw(3)]}))
    await initial.import_all()
    client = FakeClient({None: [raw(5), raw(4)], "4": [raw(4), raw(3)]})
    sync = service(config, client)
    result = await sync.sync_recent()
    assert result.boundary_found is True
    assert result.inserted == 2
    assert client.calls == [None, "4"]


@pytest.mark.asyncio
async def test_account_mismatch_stops_before_group_read(config: AppConfig) -> None:
    client = FakeClient({}, account_id="999")
    sync = service(config, client)
    with pytest.raises(AccountMismatchError, match="999"):
        await sync.verify()
    assert client.calls == []


@pytest.mark.asyncio
async def test_reconcile_gap_resumes_one_page_per_cycle(config: AppConfig) -> None:
    client = FakeClient({None: [raw(5), raw(4), raw(3)], "3": [raw(3), raw(2), raw(1)]})
    sync = service(config, client, history_since=None)
    sync._store(sync._normalize([raw(1)]))
    sync.store.update_state(
        account_id="1",
        group_id="2",
        latest_message_id="1",
        recent_ready=True,
        initial_import_complete=True,
    )

    first = await sync.sync_recent_page()
    assert first.complete is False
    assert sync.store.state("2")["reconcile_cursor"] == "3"
    assert sync.store.state("2")["latest_message_id"] == "1"

    second = await sync.sync_recent_page()
    assert second.complete is True
    state = sync.store.state("2")
    assert state["reconcile_cursor"] is None
    assert state["latest_message_id"] == "5"
    assert state["message_count"] == 5
    assert client.calls == [None, "3"]


@pytest.mark.asyncio
async def test_manager_uses_one_global_login_check_and_persists_manual_pause(
    config: AppConfig,
) -> None:
    store = MessageStore(config.database_path)
    store.whitelist_group("2", "测试群")
    client = FakeClient({None: [raw(1)]})
    manager = MultiGroupSyncManager(config, client, store)  # type: ignore[arg-type]

    await manager.run_cycle()

    assert client.login_calls == 1
    assert client.calls == [None]
    manager.pause_manual("账号冻结观察期")
    assert manager.is_active() is False
    with pytest.raises(CollectionPausedError):
        await manager.run_cycle()

    restored = MultiGroupSyncManager(config, client, store)  # type: ignore[arg-type]
    assert restored.is_active() is False
    result = await restored.resume()
    assert result["control"]["status"] == "active"
    assert restored.is_active() is True
