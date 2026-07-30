from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from qq_mcp_server.config import AppConfig
from qq_mcp_server.models import ChatMessage, GroupTarget, SyncResult
from qq_mcp_server.normalization import normalize_message, oldest_cursor
from qq_mcp_server.onebot import (
    OneBotClient,
    OneBotConfigurationError,
    OneBotSessionError,
    onebot_action_source,
)
from qq_mcp_server.store import MessageStore

LOGGER = logging.getLogger(__name__)


class AccountMismatchError(OneBotSessionError):
    pass


class CollectionPausedError(RuntimeError):
    pass


class HistoryBudgetUnavailable(RuntimeError):
    def __init__(self, budget: dict[str, Any]) -> None:
        self.budget = budget
        reason = (
            "过去 24 小时历史请求已达到安全上限"
            if budget.get("reason") == "daily_limit"
            else "历史请求全局冷却中"
        )
        super().__init__(f"{reason}；下次可请求时间 {budget.get('next_eligible_at')}")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_since(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("history_since 必须是包含时区的 ISO 8601 时间") from error
    if parsed.tzinfo is None:
        raise ValueError("history_since 必须包含时区，例如 +08:00")
    return int(parsed.timestamp())


class SyncService:
    """单群的一页最近补漏与可节流历史回填。"""

    def __init__(
        self,
        config: AppConfig,
        target: GroupTarget,
        client: OneBotClient,
        store: MessageStore,
        limiter: asyncio.Semaphore | None = None,
        *,
        history_budgeted: bool = False,
        history_source: str = "sync_service",
    ) -> None:
        self.config = config
        self.target = target
        self.client = client
        self.store = store
        self.since_timestamp = _parse_since(target.history_since or config.history_since)
        self.limiter = limiter or asyncio.Semaphore(1)
        self.history_budgeted = history_budgeted
        self.history_source = history_source

    async def verify_login(self) -> dict[str, Any]:
        async with self.limiter:
            login = await self.client.get_login_info()
        actual = str(login.get("user_id") or "")
        if actual != self.config.account_id:
            raise AccountMismatchError(
                f"NapCat 当前登录 QQ {actual or '未知'}，配置要求 {self.config.account_id}"
            )
        return login

    async def verify(self) -> dict[str, Any]:
        login = await self.verify_login()
        async with self.limiter:
            group = await self.client.get_group_info(self.target.group_id)
        self.store.update_group_name(
            self.target.group_id, str(group.get("group_name") or self.target.group_name)
        )
        return login

    async def _history(self, cursor: str | None) -> list[dict[str, Any]]:
        if self.history_budgeted:
            budget = self.store.claim_history_request(self.history_source)
            if not budget["allowed"]:
                raise HistoryBudgetUnavailable(budget)
        async with self.limiter:
            return await self.client.get_group_history(
                self.target.group_id,
                self.config.page_size,
                message_seq=cursor,
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

    async def sync_recent_page(self) -> SyncResult:
        """读取一页；超过一页的缺口通过 reconcile_cursor 在后续周期续传。"""
        state = self.store.state(self.target.group_id)
        cursor = str(state["reconcile_cursor"]) if state["reconcile_cursor"] else None
        boundary = (
            str(state["reconcile_boundary_id"])
            if state["reconcile_boundary_id"]
            else str(state["latest_message_id"])
            if state["latest_message_id"]
            else None
        )
        pending_newest = str(state["reconcile_newest_id"]) if state["reconcile_newest_id"] else None
        raw = await self._history(cursor)
        normalized = self._normalize(raw)
        text_count, inserted = self._store(normalized)
        raw_ids = {str(item.get("message_id") or "") for item in raw}

        if cursor is None and normalized:
            pending_newest = max(
                normalized, key=lambda item: (item.sent_at, item.message_id)
            ).message_id

        next_cursor = oldest_cursor(raw)
        boundary_found = boundary is None or bool(boundary and boundary in raw_ids)
        exhausted = not next_cursor or next_cursor == cursor
        complete = boundary_found or exhausted
        current_oldest = state["oldest_message_seq"]
        initial_complete = bool(state["initial_import_complete"])
        if self.since_timestamp is None:
            initial_complete = True

        self.store.update_state(
            account_id=self.config.account_id,
            group_id=self.target.group_id,
            latest_message_id=(pending_newest or boundary) if complete else None,
            oldest_message_seq=(
                next_cursor if current_oldest is None and next_cursor is not None else None
            ),
            reconcile_cursor=None if complete else next_cursor,
            reconcile_boundary_id=None if complete else boundary,
            reconcile_newest_id=None if complete else pending_newest,
            clear_reconcile=complete,
            recent_ready=True,
            initial_import_complete=initial_complete,
            error=None,
        )
        return SyncResult(
            received=len(raw),
            text_messages=text_count,
            inserted=inserted,
            pages=1,
            complete=complete,
            boundary_found=boundary_found,
        )

    async def backfill_one_page(self) -> SyncResult:
        """只回填一页旧消息；未配置每群起点时不执行深回填。"""
        state = self.store.state(self.target.group_id)
        if self.since_timestamp is None:
            self.store.update_state(
                account_id=self.config.account_id,
                group_id=self.target.group_id,
                initial_import_complete=True,
                error=None,
            )
            return SyncResult(0, 0, 0, 0, True, False)
        if not state["recent_ready"]:
            return await self.sync_recent_page()
        if state["initial_import_complete"]:
            return SyncResult(0, 0, 0, 0, True, False)

        cursor = (
            str(state["oldest_message_seq"])
            if state["oldest_message_seq"]
            else self.store.oldest_message_seq(self.target.group_id)
        )
        raw = await self._history(cursor)
        normalized = self._normalize(raw)
        reached_start = any(message.sent_at < self.since_timestamp for message in normalized)
        normalized = [message for message in normalized if message.sent_at >= self.since_timestamp]
        text_count, inserted = self._store(normalized)
        next_cursor = oldest_cursor(raw)
        complete = reached_start or not next_cursor or next_cursor == cursor
        self.store.update_state(
            account_id=self.config.account_id,
            group_id=self.target.group_id,
            oldest_message_seq=next_cursor,
            recent_ready=True,
            initial_import_complete=complete,
            error=None,
        )
        return SyncResult(
            received=len(raw),
            text_messages=text_count,
            inserted=inserted,
            pages=1,
            complete=complete,
            boundary_found=False,
        )

    async def sync_recent(self) -> SyncResult:
        await self.verify_login()
        totals = [0, 0, 0, 0]
        result = await self.sync_recent_page()
        while True:
            totals[0] += result.received
            totals[1] += result.text_messages
            totals[2] += result.inserted
            totals[3] += result.pages
            if result.complete:
                return SyncResult(
                    totals[0],
                    totals[1],
                    totals[2],
                    totals[3],
                    True,
                    result.boundary_found,
                )
            result = await self.sync_recent_page()

    async def import_all(
        self, progress: Callable[[int, int, int], None] | None = None
    ) -> SyncResult:
        await self.verify_login()
        recent = await self.sync_recent_page()
        received = recent.received
        text_total = recent.text_messages
        inserted_total = recent.inserted
        pages = recent.pages
        while self.store.state(self.target.group_id)["reconcile_cursor"]:
            current = await self.sync_recent_page()
            received += current.received
            text_total += current.text_messages
            inserted_total += current.inserted
            pages += current.pages
        while not self.store.state(self.target.group_id)["initial_import_complete"]:
            current = await self.backfill_one_page()
            received += current.received
            text_total += current.text_messages
            inserted_total += current.inserted
            pages += current.pages
            if progress:
                progress(received, inserted_total, pages)
            if current.complete:
                break
            await asyncio.sleep(
                random.uniform(
                    self.config.backfill_min_delay_seconds,
                    self.config.backfill_max_delay_seconds,
                )
            )
        return SyncResult(received, text_total, inserted_total, pages, True, False)


class MultiGroupSyncManager:
    """全局单并发调度器，并持久化账号安全熔断状态。"""

    def __init__(self, config: AppConfig, client: OneBotClient, store: MessageStore) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.limiter = asyncio.Semaphore(1)
        self._active = asyncio.Event()
        current = store.runtime_status("collection_control")
        if current.get("status") in {"paused_session", "paused_manual", "paused_configuration"}:
            return
        if current.get("status") == "active":
            self._active.set()
            return
        if config.initial_collection_paused:
            self._set_control(
                "paused_manual",
                reason="首次安全发布保持暂停",
                source="initial_configuration",
            )
        else:
            self._set_control("active", reason=None, source="initial_configuration")
            self._active.set()

    def _set_control(
        self,
        status: str,
        *,
        reason: str | None,
        source: str,
    ) -> dict[str, Any]:
        previous = self.store.runtime_status("collection_control")
        if (
            previous.get("status") == status
            and previous.get("reason") == reason
            and previous.get("source") == source
        ):
            if status == "active":
                self._active.set()
            else:
                self._active.clear()
            return previous
        value = {
            "status": status,
            "reason": reason,
            "source": source,
            "changed_at": _iso_now(),
            "revision": int(previous.get("revision") or 0) + 1,
            "last_resumed_at": (
                _iso_now() if status == "active" else previous.get("last_resumed_at")
            ),
        }
        self.store.set_runtime_status("collection_control", value)
        self.store.record_runtime_event(
            "collection_control_changed",
            {"status": status, "reason": reason, "source": source},
        )
        return self.store.runtime_status("collection_control")

    def control_status(self) -> dict[str, Any]:
        stats = getattr(self.client, "stats", None)
        return {
            **self.store.runtime_status("collection_control"),
            "onebot_actions": stats() if callable(stats) else {},
        }

    def is_active(self) -> bool:
        # API 与采集器是独立进程。数据库中的控制状态是唯一真相，
        # 本地 Event 只用于减少同一进程内等待恢复的延迟。
        active = self.store.runtime_status("collection_control").get("status") == "active"
        if active:
            self._active.set()
        else:
            self._active.clear()
        return active

    def allows_passive_events(self) -> bool:
        """掉线恢复期间仍保存被动到达的事件，但尊重人工/配置暂停。"""
        status = self.store.runtime_status("collection_control").get("status")
        return status not in {"paused_manual", "paused_configuration"}

    def require_active(self) -> None:
        if not self.is_active():
            state = self.store.runtime_status("collection_control")
            raise CollectionPausedError(
                f"QQ 采集已暂停：{state.get('reason') or state.get('status') or '未知原因'}"
            )

    async def wait_until_active(self) -> None:
        while not self.is_active():
            try:
                await asyncio.wait_for(self._active.wait(), timeout=1)
            except TimeoutError:
                continue

    def pause_manual(self, reason: str) -> dict[str, Any]:
        text = reason.strip()
        if not text:
            raise ValueError("暂停原因不能为空")
        self._active.clear()
        return self._set_control("paused_manual", reason=text[:500], source="admin_mcp")

    def pause_for(self, reason: str, *, source: str) -> dict[str, Any]:
        text = reason.strip()
        if not text:
            raise ValueError("暂停原因不能为空")
        self._active.clear()
        return self._set_control(
            "paused_manual",
            reason=text[:500],
            source=source[:80] or "internal",
        )

    def pause_session(self, error: Exception, *, source: str) -> dict[str, Any]:
        self._active.clear()
        return self._set_control(
            "paused_session",
            reason=f"{type(error).__name__}: {error}"[:500],
            source=source,
        )

    def pause_configuration(self, error: Exception, *, source: str) -> dict[str, Any]:
        self._active.clear()
        return self._set_control(
            "paused_configuration",
            reason=f"{type(error).__name__}: {error}"[:500],
            source=source,
        )

    async def verify_account(self) -> dict[str, Any]:
        async with self.limiter:
            with onebot_action_source(self.client, "manual_collection_resume"):
                login = await self.client.get_login_info()
        actual = str(login.get("user_id") or "")
        if actual != self.config.account_id:
            raise AccountMismatchError(
                f"NapCat 当前登录 QQ {actual or '未知'}，配置要求 {self.config.account_id}"
            )
        return login

    async def resume(self) -> dict[str, Any]:
        try:
            login = await self.verify_account()
        except (OneBotSessionError, AccountMismatchError) as error:
            self.pause_session(error, source="admin_resume_check")
            raise
        except OneBotConfigurationError as error:
            self.pause_configuration(error, source="admin_resume_check")
            raise
        control = self.activate_verified(source="admin_resume_check")
        return {"control": control, "login": login}

    def activate_verified(self, *, source: str) -> dict[str, Any]:
        control = self._set_control("active", reason=None, source=source)
        self._active.set()
        return control

    async def run_cycle(self) -> dict[str, Any]:
        self.require_active()
        login = await self.verify_account()
        targets = self.store.sync_targets()
        recent_results: list[dict[str, Any]] = []
        for target in targets:
            self.require_active()
            service = SyncService(
                self.config,
                target,
                self.client,
                self.store,
                self.limiter,
                history_budgeted=True,
                history_source=f"sync_cycle_recent:{target.group_id}",
            )
            result = await service.sync_recent_page()
            recent_results.append(
                {
                    "group_id": target.group_id,
                    "received": result.received,
                    "inserted": result.inserted,
                    "gap_complete": result.complete,
                }
            )

        backfill_results: list[dict[str, Any]] = []
        eligible = [
            target
            for target in targets
            if (target.history_since or self.config.history_since)
            and not self.store.state(target.group_id)["initial_import_complete"]
        ]
        pages_left = self.config.backfill_pages_per_cycle
        index = 0
        while pages_left and eligible:
            self.require_active()
            target = eligible[index % len(eligible)]
            service = SyncService(
                self.config,
                target,
                self.client,
                self.store,
                self.limiter,
                history_budgeted=True,
                history_source=f"sync_cycle_backfill:{target.group_id}",
            )
            result = await service.backfill_one_page()
            backfill_results.append(
                {
                    "group_id": target.group_id,
                    "received": result.received,
                    "inserted": result.inserted,
                    "complete": result.complete,
                }
            )
            pages_left -= 1
            if result.complete:
                eligible = [item for item in eligible if item.group_id != target.group_id]
                index = 0
            else:
                index += 1
            if pages_left and eligible:
                await asyncio.sleep(
                    random.uniform(
                        self.config.backfill_min_delay_seconds,
                        self.config.backfill_max_delay_seconds,
                    )
                )

        cycle_result: dict[str, Any] = {
            "ok": True,
            "account_id": str(login.get("user_id") or ""),
            "target_count": len(targets),
            "recent": recent_results,
            "backfill": backfill_results,
            "last_success_at": _iso_now(),
            "last_error": None,
        }
        self.store.set_runtime_status("sync_scheduler", cycle_result)
        return cycle_result

    async def run_forever(self) -> None:
        backoff = self.config.poll_interval_seconds
        while True:
            await self.wait_until_active()
            started = asyncio.get_running_loop().time()
            try:
                result = await self.run_cycle()
                LOGGER.info(
                    "全局同步完成：群=%d，最近页=%d，回填页=%d",
                    result["target_count"],
                    len(result["recent"]),
                    len(result["backfill"]),
                )
                backoff = self.config.poll_interval_seconds
            except asyncio.CancelledError:
                raise
            except (OneBotSessionError, AccountMismatchError) as error:
                LOGGER.error("检测到 QQ 会话异常，采集已熔断：%s", error)
                self.pause_session(error, source="sync_scheduler")
                continue
            except OneBotConfigurationError as error:
                LOGGER.error("OneBot 配置异常，采集已暂停：%s", error)
                self.pause_configuration(error, source="sync_scheduler")
                continue
            except Exception as error:
                LOGGER.warning("全局同步失败，%.0f 秒后退避重试：%s", backoff, error)
                previous = self.store.runtime_status("sync_scheduler")
                self.store.set_runtime_status(
                    "sync_scheduler",
                    {
                        "ok": False,
                        "last_success_at": previous.get("last_success_at"),
                        "last_error": f"{type(error).__name__}: {error}"[:500],
                        "retry_in_seconds": backoff,
                    },
                )
                self.store.record_runtime_event(
                    "sync_cycle_failed",
                    {"error": f"{type(error).__name__}: {error}"[:500]},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.config.unreachable_backoff_max_seconds)
                continue

            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, self.config.poll_interval_seconds - elapsed))
