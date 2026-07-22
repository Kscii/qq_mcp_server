from __future__ import annotations

from typing import Any

import pytest

from qq_mcp_server.config import AppConfig
from qq_mcp_server.exporter import TextExporter
from qq_mcp_server.store import MessageStore
from qq_mcp_server.sync import AccountMismatchError, SyncService


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

    async def get_login_info(self) -> dict[str, Any]:
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


def service(config: AppConfig, client: FakeClient) -> SyncService:
    store = MessageStore(config.database_path)
    exporter = TextExporter(
        store,
        group_id=config.group_id,
        group_name=config.group_name,
        path=config.export_path,
        timezone=config.timezone,
    )
    return SyncService(config, client, store, exporter)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_import_all_handles_inclusive_cursor_and_exports(config: AppConfig) -> None:
    client = FakeClient(
        {None: [raw(5), raw(4), raw(3)], "3": [raw(3), raw(2), raw(1)], "1": [raw(1)]}
    )
    sync = service(config, client)
    result = await sync.import_all()
    assert result.received == 7
    assert result.inserted == 5
    assert result.complete is True
    assert client.calls == [None, "3", "1"]
    assert sync.store.state("2")["message_count"] == 5
    assert "消息-5" in config.export_path.read_text(encoding="utf-8")


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
    assert resumed.calls == ["3", "1"]
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
