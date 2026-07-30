from __future__ import annotations

import asyncio
import logging
from typing import Any

from qq_mcp_server.config import AppConfig
from qq_mcp_server.normalization import normalize_message, oldest_cursor
from qq_mcp_server.onebot import OneBotClient, onebot_action_source
from qq_mcp_server.store import MessageStore

LOGGER = logging.getLogger(__name__)


class GapRepairService:
    """只处理人工启动的消息缺口；应用重启后默认暂停。"""

    def __init__(
        self,
        config: AppConfig,
        client: OneBotClient,
        store: MessageStore,
        *,
        interval_seconds: float = 60,
        daily_page_limit: int = 30,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.interval_seconds = interval_seconds
        self.daily_page_limit = daily_page_limit
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self.store.pause_incomplete_gap_repairs()

    def start(self, gap_id: str) -> dict[str, Any]:
        gap = self.store.refresh_message_gap_boundaries(gap_id)
        if gap["end_at"] is None:
            raise ValueError("缺口尚未结束，不能开始历史修复")
        if gap["status"] in {"repaired", "accepted"}:
            raise ValueError("该缺口已经结束处理")
        automatic_jobs = self.store.list_recovery_jobs(
            group_id=str(gap["group_id"]),
            active_only=True,
            limit=20,
        )
        for job in automatic_jobs:
            self.store.update_recovery_job(
                str(job["job_id"]),
                status="cancelled",
                error="用户启动人工缺口修复，人工操作优先",
            )
        budget = self.store.history_request_budget(daily_limit=self.daily_page_limit)
        if budget["remaining"] <= 0:
            raise ValueError("过去 24 小时历史请求已达到 30 页安全上限")
        updated = self.store.update_message_gap_repair(
            gap_id,
            status="repairing",
            repair_cursor=gap["repair_cursor"],
            error=None,
        )
        self._wake.set()
        return updated

    def pause(self, gap_id: str) -> dict[str, Any]:
        gap = self.store.message_gap(gap_id)
        if gap["status"] != "repairing":
            raise ValueError("只有正在修复的缺口可以暂停")
        return self.store.update_message_gap_repair(
            gap_id,
            status="paused",
            repair_cursor=gap["repair_cursor"],
            error="用户暂停",
        )

    async def repair_one_page(self, gap_id: str) -> dict[str, Any]:
        async with self._lock:
            gap = self.store.refresh_message_gap_boundaries(gap_id)
            if gap["status"] != "repairing":
                return gap
            group_id = str(gap["group_id"])
            cursor = str(gap["repair_cursor"]) if gap["repair_cursor"] else None
            if cursor is None and gap["after_message_id"]:
                cursor = self.store.message_seq(group_id, str(gap["after_message_id"]))
            budget = self.store.claim_history_request(
                "manual_gap_repair",
                daily_limit=self.daily_page_limit,
            )
            if not budget["allowed"]:
                return self.store.update_message_gap_repair(
                    gap_id,
                    status="repairing",
                    repair_cursor=cursor,
                    error=(
                        "历史请求等待安全额度；下次可请求时间 " + str(budget["next_eligible_at"])
                    ),
                )
            try:
                with onebot_action_source(self.client, "manual_gap_repair"):
                    raw = await self.client.get_group_history(
                        group_id,
                        self.config.page_size,
                        message_seq=cursor,
                    )
                messages = []
                for item in raw:
                    message = normalize_message(item, expected_group_id=group_id)
                    if message is not None:
                        messages.append(message)
                self.store.upsert(messages)
                raw_ids = {str(item.get("message_id") or "") for item in raw}
                before_id = str(gap["before_message_id"] or "")
                after_id = str(gap["after_message_id"] or "")
                boundary_found = bool(before_id and before_id in raw_ids and after_id)
                next_cursor = oldest_cursor(raw)
                exhausted = not raw or not next_cursor or next_cursor == cursor
                if boundary_found:
                    status = "repaired"
                    error = None
                    next_cursor = None
                elif exhausted:
                    status = "unverified"
                    error = "历史接口已耗尽，未能同时验证缺口前后边界"
                    next_cursor = None
                else:
                    status = "repairing"
                    error = None
                return self.store.update_message_gap_repair(
                    gap_id,
                    status=status,
                    repair_cursor=next_cursor,
                    increment_pages=True,
                    error=error,
                )
            except Exception as error:
                LOGGER.warning("缺口 %s 修复失败并暂停：%s", gap_id, error)
                return self.store.update_message_gap_repair(
                    gap_id,
                    status="paused",
                    repair_cursor=cursor,
                    error=f"{type(error).__name__}: {error}",
                )

    async def run_forever(self) -> None:
        while True:
            await self._wake.wait()
            repairing = [
                gap
                for gap in self.store.list_message_gaps(unresolved_only=True)
                if gap["status"] == "repairing"
            ]
            if not repairing:
                self._wake.clear()
                continue
            await self.repair_one_page(str(repairing[0]["gap_id"]))
            # 无论本页是否刚好完成一个缺口，都保持全局页间隔，避免多个缺口
            # 在同一分钟内连续触发历史接口。
            await asyncio.sleep(self.interval_seconds)
