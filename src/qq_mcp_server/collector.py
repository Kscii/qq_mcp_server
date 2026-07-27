from __future__ import annotations

import json
import logging
import secrets

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from qq_mcp_server.runtime import NapCatRuntime

LOGGER = logging.getLogger(__name__)
COLLECTOR_HOST = "127.0.0.1"
COLLECTOR_PORT = 3001
COLLECTOR_PATH = "/onebot/v11/ws"


def _authorized(websocket: WebSocket, expected_token: str) -> bool:
    authorization = websocket.headers.get("authorization", "")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return False
    return secrets.compare_digest(authorization[len(prefix) :], expected_token)


def create_collector_app(runtime: NapCatRuntime, token: str) -> Starlette:
    async def onebot_events(websocket: WebSocket) -> None:
        if not _authorized(websocket, token):
            await websocket.close(code=1008, reason="OneBot Token 无效")
            return
        await websocket.accept()
        session_id = runtime.begin_event_session()
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    LOGGER.warning("忽略无效 OneBot WebSocket JSON")
                    continue
                if not isinstance(event, dict):
                    continue
                # 反向 WebSocket 也可能承载 action 响应；采集器只接受事件。
                if not isinstance(event.get("post_type"), str):
                    continue
                runtime.record_event_received()
                await runtime.handle_event(event)
        except WebSocketDisconnect as error:
            runtime.end_event_session(
                reason=f"websocket_disconnect:{error.code}",
                open_gap=True,
                session_id=session_id,
            )
        except Exception as error:
            runtime.end_event_session(
                reason=f"{type(error).__name__}: {error}"[:500],
                open_gap=True,
                session_id=session_id,
            )
            raise

    return Starlette(
        routes=[WebSocketRoute(COLLECTOR_PATH, onebot_events)],
    )
