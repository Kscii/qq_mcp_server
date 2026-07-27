from __future__ import annotations

import json
from typing import Any

from starlette.websockets import WebSocketDisconnect

from qq_mcp_server.collector import create_collector_app
from qq_mcp_server.config import AppConfig
from qq_mcp_server.runtime import NapCatRuntime
from qq_mcp_server.store import MessageStore


class CollectorClient:
    async def get_status(self) -> dict[str, Any]:
        return {"online": True, "good": True}


class FakeWebSocket:
    def __init__(self, token: str | None, messages: list[dict[str, Any]] | None = None) -> None:
        self.headers = {"authorization": f"Bearer {token}"} if token is not None else {}
        self.messages = [json.dumps(message) for message in messages or []]
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def receive_text(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        raise WebSocketDisconnect(code=1000)


def _event() -> dict[str, Any]:
    return {
        "time": 10,
        "self_id": "1",
        "post_type": "message",
        "message_type": "group",
        "group_id": "2",
        "user_id": "9",
        "message_id": "10",
        "message_seq": "10",
        "sender": {"nickname": "玩家", "card": "角色"},
        "message": [{"type": "text", "data": {"text": "WebSocket 消息"}}],
    }


async def test_reverse_websocket_requires_token_and_stores_event(config: AppConfig) -> None:
    store = MessageStore(config.database_path)
    runtime = NapCatRuntime(
        config,
        CollectorClient(),  # type: ignore[arg-type]
        store,
        "secret",
        collector_owner=True,
    )
    app = create_collector_app(runtime, "secret")
    endpoint = app.routes[0].endpoint  # type: ignore[attr-defined]

    rejected = FakeWebSocket(None)
    await endpoint(rejected)
    assert rejected.closed == (1008, "OneBot Token 无效")

    accepted = FakeWebSocket("secret", [_event()])
    await endpoint(accepted)
    assert accepted.accepted is True

    assert store.state("2")["message_count"] == 1
    transport = store.runtime_status("event_transport")
    assert transport["transport"] == "reverse_websocket"
    assert transport["connected"] is False
