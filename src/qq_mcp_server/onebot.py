from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx


class OneBotError(RuntimeError):
    pass


class OneBotClient:
    """最小只读 OneBot 11 客户端；不存在发送动作或任意动作入口。"""

    _ALLOWED_ACTIONS = frozenset({"get_login_info", "get_group_info", "get_group_msg_history"})

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        request_timeout: float = 20,
        history_timeout: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("ONEBOT_ACCESS_TOKEN 不能为空")
        self._history_timeout = history_timeout
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=request_timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _action(
        self, action: str, payload: dict[str, Any] | None = None, *, history: bool = False
    ) -> Any:
        if action not in self._ALLOWED_ACTIONS:
            raise OneBotError(f"拒绝调用非只读 OneBot 动作：{action}")
        attempts = 3
        for attempt in range(attempts):
            try:
                response = await self._client.post(
                    f"/{action}",
                    json=payload or {},
                    timeout=self._history_timeout if history else None,
                )
                response.raise_for_status()
                body = response.json()
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
                if attempt + 1 == attempts:
                    raise OneBotError(f"{action} 连接失败：{type(error).__name__}") from error
                await asyncio.sleep(0.5 * (2**attempt))
            except httpx.ReadTimeout as error:
                # 不立刻重复历史请求，避免 NapCat 仍处理上一次请求时形成堆积。
                raise OneBotError(f"{action} 读取超时") from error
            except (httpx.HTTPError, json.JSONDecodeError) as error:
                raise OneBotError(f"{action} 请求失败：{type(error).__name__}") from error
        else:  # pragma: no cover
            raise AssertionError("重试循环未执行")
        if not isinstance(body, dict):
            raise OneBotError(f"{action} 返回的不是对象")
        if body.get("status") != "ok" or body.get("retcode") != 0:
            message = body.get("wording") or body.get("message") or "未知错误"
            raise OneBotError(f"{action} 失败：{message}")
        return body.get("data")

    async def get_login_info(self) -> dict[str, Any]:
        data = await self._action("get_login_info")
        if not isinstance(data, dict):
            raise OneBotError("get_login_info 返回格式错误")
        return data

    async def get_group_info(self, group_id: str) -> dict[str, Any]:
        data = await self._action("get_group_info", {"group_id": group_id, "no_cache": False})
        if not isinstance(data, dict):
            raise OneBotError("get_group_info 返回格式错误")
        if str(data.get("group_id") or "") != group_id:
            raise OneBotError("get_group_info 返回了非目标群")
        return data

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
