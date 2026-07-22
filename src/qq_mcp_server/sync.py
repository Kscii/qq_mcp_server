from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from qq_mcp_server.config import AppConfig
from qq_mcp_server.models import ChatMessage, GroupTarget, SyncResult
from qq_mcp_server.normalization import normalize_message, oldest_cursor
from qq_mcp_server.onebot import OneBotClient
from qq_mcp_server.store import MessageStore

LOGGER = logging.getLogger(__name__)


class AccountMismatchError(RuntimeError):
    pass


def _parse_since(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("qq.history_since 必须是包含时区的 ISO 8601 时间") from error
    if parsed.tzinfo is None:
        raise ValueError("qq.history_since 必须包含时区，例如 +08:00")
    return int(parsed.timestamp())


class SyncService:
    """一个白名单群的最近同步和可中断历史回填。"""

    def __init__(
        self,
        config: AppConfig,
        target: GroupTarget,
        client: OneBotClient,
        store: MessageStore,
        limiter: asyncio.Semaphore | None = None,
    ) -> None:
        self.config = config
        self.target = target
        self.client = client
        self.store = store
        self.since_timestamp = _parse_since(config.history_since)
        self.limiter = limiter or asyncio.Semaphore(config.sync_concurrency)

    async def verify(self) -> dict[str, Any]:
        async with self.limiter:
            login = await self.client.get_login_info()
        actual = str(login.get("user_id") or "")
        if actual != self.config.account_id:
            raise AccountMismatchError(
                f"NapCat 当前登录 QQ {actual}，配置要求 {self.config.account_id}"
            )
        async with self.limiter:
            group = await self.client.get_group_info(self.target.group_id)
        self.store.update_group_name(
            self.target.group_id, str(group.get("group_name") or self.target.group_name)
        )
        return login

    async def _history(self, cursor: str | None) -> list[dict[str, Any]]:
        async with self.limiter:
            return await self.client.get_group_history(
                self.target.group_id, self.config.page_size, message_seq=cursor
            )

    def _normalize(self, raw_messages: list[dict[str, Any]]) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for raw in raw_messages:
            message = normalize_message(raw, expected_group_id=self.target.group_id)
            if message is not None:
                messages.append(message)
        return messages

    def _store(self, messages: list[ChatMessage]) -> tuple[int, int]:
        return self.store.upsert(messages)

    async def import_all(
        self, progress: Callable[[int, int, int], None] | None = None
    ) -> SyncResult:
        await self.verify()
        state = self.store.state(self.target.group_id)
        if state["initial_import_complete"]:
            return await self.sync_recent()
        cursor = self.store.oldest_message_seq(self.target.group_id)
        previous_cursor: str | None = None
        received = text_total = inserted_total = pages = 0
        complete = False
        newest_id = self.store.latest_message_id(self.target.group_id)

        while True:
            raw = await self._history(cursor)
            pages += 1
            received += len(raw)
            normalized = self._normalize(raw)
            reached_start = False
            if self.since_timestamp is not None:
                reached_start = any(
                    message.sent_at < self.since_timestamp for message in normalized
                )
                normalized = [
                    message for message in normalized if message.sent_at >= self.since_timestamp
                ]
            text_count, inserted = self._store(normalized)
            text_total += text_count
            inserted_total += inserted
            if normalized and newest_id is None:
                newest = max(normalized, key=lambda item: (item.sent_at, item.message_id))
                newest_id = newest.message_id
            previous_cursor, cursor = cursor, oldest_cursor(raw)
            complete = reached_start or not cursor or cursor == previous_cursor
            self.store.update_state(
                account_id=self.config.account_id,
                group_id=self.target.group_id,
                latest_message_id=newest_id,
                oldest_message_seq=cursor,
                recent_ready=True,
                initial_import_complete=complete,
                error=None,
            )
            if progress:
                progress(received, inserted_total, pages)
            if complete:
                break
            await asyncio.sleep(0)

        return SyncResult(received, text_total, inserted_total, pages, complete, False)

    async def sync_recent(self) -> SyncResult:
        await self.verify()
        boundary = self.store.latest_message_id(self.target.group_id)
        cursor: str | None = None
        previous_cursor: str | None = None
        received = text_total = inserted_total = pages = 0
        boundary_found = boundary is None
        newest_id = boundary

        while True:
            raw = await self._history(cursor)
            pages += 1
            received += len(raw)
            raw_ids = {str(item.get("message_id") or "") for item in raw}
            boundary_found = boundary_found or bool(boundary and boundary in raw_ids)
            normalized = self._normalize(raw)
            text_count, inserted = self._store(normalized)
            text_total += text_count
            inserted_total += inserted
            if normalized and pages == 1:
                newest_id = max(
                    normalized, key=lambda item: (item.sent_at, item.message_id)
                ).message_id
            previous_cursor, cursor = cursor, oldest_cursor(raw)
            exhausted = not cursor or cursor == previous_cursor
            if boundary_found or exhausted:
                break

        self.store.update_state(
            account_id=self.config.account_id,
            group_id=self.target.group_id,
            latest_message_id=newest_id,
            recent_ready=True,
            error=None,
        )
        return SyncResult(received, text_total, inserted_total, pages, True, boundary_found)

    async def run_forever(self) -> None:
        while True:
            try:
                state = self.store.state(self.target.group_id)
                # 首次只取最近一页，先让群 App 可用；之后在后台完整向前回填。
                if not state["recent_ready"]:
                    recent = await self.sync_recent()
                    LOGGER.info(
                        "群 %s 最近消息就绪：页=%d，新增=%d",
                        self.target.group_id,
                        recent.pages,
                        recent.inserted,
                    )
                    state = self.store.state(self.target.group_id)
                result = (
                    await self.sync_recent()
                    if state["initial_import_complete"]
                    else await self.import_all()
                )
                LOGGER.info(
                    "群 %s 同步完成：页=%d，收到=%d，新增=%d",
                    self.target.group_id,
                    result.pages,
                    result.received,
                    result.inserted,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("群 %s 同步失败：%s", self.target.group_id, error)
                self.store.record_error(
                    account_id=self.config.account_id,
                    group_id=self.target.group_id,
                    error=f"{type(error).__name__}: {error}",
                )
            await asyncio.sleep(self.config.poll_interval_seconds)


class MultiGroupSyncManager:
    """动态跟随 WebUI 白名单启动和停止每群同步任务。"""

    def __init__(self, config: AppConfig, client: OneBotClient, store: MessageStore) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.limiter = asyncio.Semaphore(config.sync_concurrency)
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def run_forever(self) -> None:
        try:
            while True:
                targets = {target.group_key: target for target in self.store.sync_targets()}
                for group_key, target in targets.items():
                    task = self.tasks.get(group_key)
                    if task is None or task.done():
                        service = SyncService(
                            self.config, target, self.client, self.store, self.limiter
                        )
                        self.tasks[group_key] = asyncio.create_task(
                            service.run_forever(), name=f"qq-sync-{group_key}"
                        )
                for group_key in set(self.tasks) - set(targets):
                    self.tasks.pop(group_key).cancel()
                await asyncio.sleep(self.config.registry_refresh_seconds)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
