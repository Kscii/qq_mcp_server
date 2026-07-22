from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from qq_mcp_server.cards import CharacterCardService
from qq_mcp_server.config import AppConfig, ConfigError
from qq_mcp_server.mcp_server import create_http_app, create_mcp_servers
from qq_mcp_server.onebot import OneBotClient
from qq_mcp_server.rules import RuleIndex
from qq_mcp_server.store import MessageStore
from qq_mcp_server.sync import MultiGroupSyncManager


def build_services(
    config: AppConfig,
) -> tuple[
    OneBotClient,
    MessageStore,
    MultiGroupSyncManager,
    RuleIndex,
    CharacterCardService,
]:
    token = os.environ.get("ONEBOT_ACCESS_TOKEN", "").strip()
    if not token:
        raise ConfigError("缺少环境变量 ONEBOT_ACCESS_TOKEN")
    store = MessageStore(config.database_path)
    client = OneBotClient(
        config.onebot_url,
        token,
        request_timeout=config.request_timeout_seconds,
        history_timeout=config.history_timeout_seconds,
    )
    manager = MultiGroupSyncManager(config, client, store)
    rules = RuleIndex(config.rules_database_path)
    cards = CharacterCardService(store, config.card_storage_dir)
    return client, store, manager, rules, cards


async def run_server(config: AppConfig) -> None:
    # Google token validation sends the access token as a tokeninfo query
    # parameter. httpx's INFO request log includes the full URL, so keep it out
    # of production logs while retaining warnings and errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    client, store, manager, rules, cards = build_services(config)
    admin, group = create_mcp_servers(config, store, client, rules, cards)
    app = create_http_app(admin, group, store)
    logging.getLogger(__name__).info(
        "Admin MCP: %s:%d/mcp/admin；每群 MCP: /mcp/groups/{group_key}",
        config.host,
        config.port,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
            lifespan="on",
        )
    )
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(manager.run_forever())
            tasks.create_task(server.serve())
    finally:
        await client.close()
