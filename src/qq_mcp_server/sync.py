from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from qq_mcp_server.config import AppConfig
from qq_mcp_server.exporter import TextExporter
from qq_mcp_server.models import ChatMessage, SyncResult
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
    def __init__(
        self,
        config: AppConfig,
        client: OneBotClient,
        store: MessageStore,
        exporter: TextExporter,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.exporter = exporter
        self.since_timestamp = _parse_since(config.history_since)

    async def verify(self) -> dict[str, Any]:
        login = await self.client.get_login_info()
        actual = str(login.get("user_id") or "")
        if actual != self.config.account_id:
            raise AccountMismatchError(
                f"NapCat 当前登录 QQ {actual}，配置要求 {self.config.account_id}"
            )
        await self.client.get_group_info(self.config.group_id)
        return login

    def _normalize(self, raw_messages: list[dict[str, Any]]) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for raw in raw_messages:
            message = normalize_message(raw, expected_group_id=self.config.group_id)
            if message is not None:
                messages.append(message)
        return messages

    def _store(self, messages: list[ChatMessage]) -> tuple[int, int]:
        text_count, inserted = self.store.upsert(messages)
        if inserted:
            self.exporter.write()
        return text_count, inserted

    async def import_all(
        self, progress: Callable[[int, int, int], None] | None = None
    ) -> SyncResult:
        await self.verify()
        state = self.store.state(self.config.group_id)
        if state["initial_import_complete"]:
            return await self.sync_recent()
        cursor = self.store.oldest_message_seq(self.config.group_id)
        previous_cursor: str | None = None
        received = text_total = inserted_total = pages = 0
        complete = False
        newest_id: str | None = self.store.latest_message_id(self.config.group_id)

        while True:
            raw = await self.client.get_group_history(
                self.config.group_id, self.config.page_size, message_seq=cursor
            )
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
                group_id=self.config.group_id,
                latest_message_id=newest_id,
                oldest_message_seq=cursor,
                initial_import_complete=complete,
            )
            if progress:
                progress(received, inserted_total, pages)
            if complete:
                break

        if not self.config.export_path.exists():
            self.exporter.write()
        return SyncResult(received, text_total, inserted_total, pages, complete, False)

    async def sync_recent(self) -> SyncResult:
        await self.verify()
        boundary = self.store.latest_message_id(self.config.group_id)
        cursor: str | None = None
        previous_cursor: str | None = None
        received = text_total = inserted_total = pages = 0
        boundary_found = boundary is None
        newest_id: str | None = boundary

        while True:
            raw = await self.client.get_group_history(
                self.config.group_id, self.config.page_size, message_seq=cursor
            )
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
            group_id=self.config.group_id,
            latest_message_id=newest_id,
            error=None,
        )
        return SyncResult(received, text_total, inserted_total, pages, True, boundary_found)

    async def run_forever(self) -> None:
        delay = self.config.poll_interval_seconds
        while True:
            try:
                state = self.store.state(self.config.group_id)
                result = (
                    await self.sync_recent()
                    if state["initial_import_complete"]
                    else await self.import_all()
                )
                LOGGER.info(
                    "同步完成：页=%d，收到=%d，新增=%d",
                    result.pages,
                    result.received,
                    result.inserted,
                )
                delay = self.config.poll_interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("同步失败：%s", error)
                self.store.record_error(
                    account_id=self.config.account_id,
                    group_id=self.config.group_id,
                    error=f"{type(error).__name__}: {error}",
                )
                delay = min(max(delay * 2, 15), 300)
            await asyncio.sleep(delay)
