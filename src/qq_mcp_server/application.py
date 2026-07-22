from __future__ import annotations

import asyncio
import logging
import os

from qq_mcp_server.config import AppConfig, ConfigError
from qq_mcp_server.exporter import TextExporter
from qq_mcp_server.mcp_server import create_mcp
from qq_mcp_server.onebot import OneBotClient
from qq_mcp_server.store import MessageStore
from qq_mcp_server.sync import SyncService


def build_services(
    config: AppConfig,
) -> tuple[OneBotClient, MessageStore, TextExporter, SyncService]:
    token = os.environ.get("ONEBOT_ACCESS_TOKEN", "").strip()
    if not token:
        raise ConfigError("缺少环境变量 ONEBOT_ACCESS_TOKEN")
    store = MessageStore(config.database_path)
    exporter = TextExporter(
        store,
        group_id=config.group_id,
        group_name=config.group_name,
        path=config.export_path,
        timezone=config.timezone,
    )
    client = OneBotClient(
        config.onebot_url,
        token,
        request_timeout=config.request_timeout_seconds,
        history_timeout=config.history_timeout_seconds,
    )
    return client, store, exporter, SyncService(config, client, store, exporter)


async def run_server(config: AppConfig) -> None:
    client, store, _, sync = build_services(config)
    mcp = create_mcp(config, store)
    logging.getLogger(__name__).info(
        "MCP 监听 %s:%d/mcp；目标群 %s",
        config.host,
        config.port,
        config.group_id,
    )
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(sync.run_forever())
            tasks.create_task(
                mcp.run_http_async(
                    host=config.host,
                    port=config.port,
                    path="/mcp",
                    show_banner=False,
                    log_level="info",
                    stateless_http=True,
                )
            )
    finally:
        await client.close()
