from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import httpx


class OneBotError(RuntimeError):
    pass


class OneBotTransportError(OneBotError):
    pass


class OneBotSessionError(OneBotError):
    pass


class OneBotConfigurationError(OneBotError):
    pass


_SESSION_ERROR_MARKERS = (
    "未登录",
    "登录状态",
    "登录已失效",
    "登录失效",
    "kickedoffline",
    "kicked off",
    "not logged",
    "login required",
)


@contextmanager
def onebot_action_source(client: object, source: str) -> Iterator[None]:
    """为真实客户端标注审计来源；测试替身和兼容客户端安全降级。"""
    factory = getattr(client, "action_source", None)
    if not callable(factory):
        yield
        return
    with factory(source):
        yield


class OneBotClient:
    """最小只读 OneBot 11 客户端；不存在发送动作或任意动作入口。"""

    _ALLOWED_ACTIONS = frozenset(
        {
            "get_status",
            "get_login_info",
            "get_group_info",
            "get_group_list",
            "get_group_member_list",
            "get_group_msg_history",
        }
    )

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        request_timeout: float = 20,
        history_timeout: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
        audit_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not token:
            raise ValueError("ONEBOT_ACCESS_TOKEN 不能为空")
        self._history_timeout = history_timeout
        self._audit_hook = audit_hook
        self._action_source: ContextVar[str] = ContextVar(
            "onebot_action_source", default="unspecified"
        )
        self._stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "calls": 0,
                "successes": 0,
                "errors": 0,
                "total_latency_ms": 0.0,
                "last_called_at": None,
                "last_error": None,
            }
        )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=request_timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @contextmanager
    def action_source(self, source: str) -> Iterator[None]:
        text = source.strip()[:80]
        if not text:
            raise ValueError("OneBot 调用来源不能为空")
        token = self._action_source.set(text)
        try:
            yield
        finally:
            self._action_source.reset(token)

    async def _action(
        self, action: str, payload: dict[str, Any] | None = None, *, history: bool = False
    ) -> Any:
        started = time.monotonic()
        try:
            result = await self._execute_action(action, payload, history=history)
        except Exception as error:
            self._record_audit(
                action,
                outcome="error",
                latency_ms=(time.monotonic() - started) * 1000,
                error_type=type(error).__name__,
            )
            raise
        self._record_audit(
            action,
            outcome="success",
            latency_ms=(time.monotonic() - started) * 1000,
            error_type=None,
        )
        return result

    def _record_audit(
        self,
        action: str,
        *,
        outcome: str,
        latency_ms: float,
        error_type: str | None,
    ) -> None:
        if self._audit_hook is None:
            return
        self._audit_hook(
            {
                "action": action,
                "source": self._action_source.get(),
                "outcome": outcome,
                "latency_ms": round(latency_ms, 3),
                "error_type": error_type,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    async def _execute_action(
        self, action: str, payload: dict[str, Any] | None = None, *, history: bool = False
    ) -> Any:
        if action not in self._ALLOWED_ACTIONS:
            raise OneBotError(f"拒绝调用非只读 OneBot 动作：{action}")
        started = time.monotonic()
        stats = self._stats[action]
        stats["calls"] += 1
        stats["last_called_at"] = datetime.now(UTC).isoformat()
        # get_status 只读取 NapCat 本地 selfInfo.online。它用于安全熔断，
        # 不需要像普通只读动作一样在本机端口不可达时快速连重三次。
        attempts = 1 if action == "get_status" else 3
        for attempt in range(attempts):
            try:
                response = await self._client.post(
                    f"/{action}",
                    json=payload or {},
                    timeout=self._history_timeout if history else None,
                )
                if response.status_code in {401, 403}:
                    raise OneBotConfigurationError(f"{action} OneBot Token 或访问控制配置错误")
                response.raise_for_status()
                body = response.json()
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
                if attempt + 1 == attempts:
                    selected_error = OneBotTransportError(
                        f"{action} 连接失败：{type(error).__name__}"
                    )
                    stats["errors"] += 1
                    stats["last_error"] = str(selected_error)
                    stats["total_latency_ms"] += (time.monotonic() - started) * 1000
                    raise selected_error from error
                await asyncio.sleep(0.5 * (2**attempt))
            except httpx.ReadTimeout as error:
                # 不立刻重复历史请求，避免 NapCat 仍处理上一次请求时形成堆积。
                selected_error = OneBotTransportError(f"{action} 读取超时")
                stats["errors"] += 1
                stats["last_error"] = str(selected_error)
                stats["total_latency_ms"] += (time.monotonic() - started) * 1000
                raise selected_error from error
            except OneBotConfigurationError as error:
                stats["errors"] += 1
                stats["last_error"] = str(error)
                stats["total_latency_ms"] += (time.monotonic() - started) * 1000
                raise
            except (httpx.HTTPError, json.JSONDecodeError) as error:
                selected_error = OneBotTransportError(f"{action} 请求失败：{type(error).__name__}")
                stats["errors"] += 1
                stats["last_error"] = str(selected_error)
                stats["total_latency_ms"] += (time.monotonic() - started) * 1000
                raise selected_error from error
        else:  # pragma: no cover
            raise AssertionError("重试循环未执行")
        if not isinstance(body, dict):
            raise OneBotError(f"{action} 返回的不是对象")
        if body.get("status") != "ok" or body.get("retcode") != 0:
            message = body.get("wording") or body.get("message") or "未知错误"
            text = f"{action} 失败：{message}"
            error_type = (
                OneBotSessionError
                if any(marker in text.lower() for marker in _SESSION_ERROR_MARKERS)
                else OneBotError
            )
            response_error: OneBotError = error_type(text)
            stats["errors"] += 1
            stats["last_error"] = text
            stats["total_latency_ms"] += (time.monotonic() - started) * 1000
            raise response_error
        stats["successes"] += 1
        stats["last_error"] = None
        stats["total_latency_ms"] += (time.monotonic() - started) * 1000
        return body.get("data")

    def stats(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for action, value in self._stats.items():
            calls = int(value["calls"])
            result[action] = {
                **value,
                "average_latency_ms": (
                    round(float(value["total_latency_ms"]) / calls, 3) if calls else None
                ),
                "total_latency_ms": round(float(value["total_latency_ms"]), 3),
            }
        return result

    async def get_status(self) -> dict[str, Any]:
        data = await self._action("get_status")
        if not isinstance(data, dict):
            raise OneBotError("get_status 返回格式错误")
        online = data.get("online")
        good = data.get("good")
        return {
            **data,
            "online": online if isinstance(online, bool) else False,
            "good": good if isinstance(good, bool) else True,
        }

    async def get_login_info(self) -> dict[str, Any]:
        data = await self._action("get_login_info")
        if not isinstance(data, dict):
            raise OneBotError("get_login_info 返回格式错误")
        return data

    async def get_group_info(self, group_id: str, *, no_cache: bool = False) -> dict[str, Any]:
        data = await self._action("get_group_info", {"group_id": group_id, "no_cache": no_cache})
        if not isinstance(data, dict):
            raise OneBotError("get_group_info 返回格式错误")
        if str(data.get("group_id") or "") != group_id:
            raise OneBotError("get_group_info 返回了非目标群")
        return data

    async def get_group_list(self) -> list[dict[str, Any]]:
        data = await self._action("get_group_list", {"no_cache": True})
        if not isinstance(data, list):
            raise OneBotError("get_group_list 返回格式错误")
        result: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            group_id = str(item.get("group_id") or "")
            if group_id.isdigit():
                result.append(
                    {
                        "group_id": group_id,
                        "group_name": str(item.get("group_name") or group_id),
                        "member_count": int(item.get("member_count") or 0),
                        "max_member_count": int(item.get("max_member_count") or 0),
                    }
                )
        return result

    async def get_group_member_list(
        self, group_id: str, *, no_cache: bool = False
    ) -> list[dict[str, Any]]:
        data = await self._action(
            "get_group_member_list", {"group_id": group_id, "no_cache": no_cache}
        )
        if not isinstance(data, list):
            raise OneBotError("get_group_member_list 返回格式错误")
        result: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict) or str(item.get("group_id") or group_id) != group_id:
                continue
            user_id = str(item.get("user_id") or "")
            if not user_id.isdigit():
                continue
            nickname = str(item.get("nickname") or "")
            card = str(item.get("card") or "")
            result.append(
                {
                    "qq_user_id": user_id,
                    "display_name": card or nickname or user_id,
                    "card": card,
                    "nickname": nickname,
                    "onebot_role": str(item.get("role") or "member"),
                }
            )
        return result

    async def get_group_history(
        self, group_id: str, count: int, *, message_seq: str | None = None
    ) -> list[dict[str, Any]]:
        if not 1 <= count <= 500:
            raise ValueError("count 必须在 1 到 500 之间")
        payload: dict[str, Any] = {
            "group_id": group_id,
            "count": count,
            "reverse_order": True,
            "disable_get_url": True,
            "parse_mult_msg": False,
            "quick_reply": False,
        }
        if message_seq:
            payload["message_seq"] = message_seq
        data = await self._action("get_group_msg_history", payload, history=True)
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            raise OneBotError("get_group_msg_history 返回格式错误")
        return [item for item in messages if isinstance(item, dict)]
