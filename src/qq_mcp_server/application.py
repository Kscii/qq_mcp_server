from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import uvicorn

from qq_mcp_server.cards import CharacterCardService
from qq_mcp_server.collector import (
    COLLECTOR_HOST,
    COLLECTOR_PORT,
    create_collector_app,
)
from qq_mcp_server.config import AppConfig, ConfigError
from qq_mcp_server.gaps import GapRepairService
from qq_mcp_server.mcp_server import create_http_app, create_mcp_servers
from qq_mcp_server.onebot import OneBotClient
from qq_mcp_server.rules import RuleIndex
from qq_mcp_server.runtime import NapCatRuntime
from qq_mcp_server.store import MessageStore
from qq_mcp_server.sync import MultiGroupSyncManager


def build_services(
    config: AppConfig,
    *,
    collector_owner: bool = False,
) -> tuple[
    OneBotClient,
    MessageStore,
    MultiGroupSyncManager,
    RuleIndex,
    CharacterCardService,
    NapCatRuntime,
    GapRepairService,
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
        audit_hook=store.record_onebot_action,
    )
    manager = MultiGroupSyncManager(config, client, store)
    runtime = NapCatRuntime(
        config,
        client,
        store,
        token,
        manager,
        collector_owner=collector_owner,
    )
    gap_repair = GapRepairService(config, client, store)
    rules = RuleIndex(config.rules_database_path)
    cards = CharacterCardService(store, config.card_storage_dir)
    return client, store, manager, rules, cards, runtime, gap_repair


def _server(app: Any, *, host: str, port: int, log_level: str = "info") -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level,
            lifespan="on",
        )
    )


def _quiet_httpx_logs() -> None:
    # Google token validation sends the access token as a tokeninfo query
    # parameter. httpx's INFO request log includes the full URL, so keep it out
    # of production logs while retaining warnings and errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_api_server(config: AppConfig) -> None:
    _quiet_httpx_logs()
    client, store, manager, rules, cards, runtime, gap_repair = build_services(config)
    admin, group = create_mcp_servers(
        config,
        store,
        client,
        rules,
        cards,
        runtime=runtime,
        gap_repair=gap_repair,
    )
    app = create_http_app(admin, group, store)
    logging.getLogger(__name__).info(
        "Admin MCP: %s:%d/mcp/admin；每群 MCP: /mcp/groups/{group_key}",
        config.host,
        config.port,
    )
    server = _server(app, host=config.host, port=config.port)
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(gap_repair.run_forever())
            tasks.create_task(server.serve())
    finally:
        await client.close()


async def run_collector_server(config: AppConfig) -> None:
    _quiet_httpx_logs()
    client, _store, _manager, _rules, _cards, runtime, _gap_repair = build_services(
        config,
        collector_owner=True,
    )
    token = os.environ.get("ONEBOT_ACCESS_TOKEN", "").strip()
    app = create_collector_app(runtime, token)
    server = _server(
        app,
        host=COLLECTOR_HOST,
        port=COLLECTOR_PORT,
        log_level="warning",
    )
    logging.getLogger(__name__).info(
        "OneBot 反向 WebSocket 采集器：ws://%s:%d/onebot/v11/ws",
        COLLECTOR_HOST,
        COLLECTOR_PORT,
    )
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(runtime.run_watchdog_forever())
            tasks.create_task(server.serve())
    finally:
        runtime.end_event_session(
            reason="collector_shutdown",
            open_gap=True,
            confidence="suspected",
        )
        await client.close()


async def run_server(config: AppConfig) -> None:
    """兼容本地单进程启动；生产部署使用 run-api 与 run-collector。"""
    _quiet_httpx_logs()
    client, store, manager, rules, cards, runtime, gap_repair = build_services(
        config,
        collector_owner=True,
    )
    admin, group = create_mcp_servers(
        config,
        store,
        client,
        rules,
        cards,
        runtime=runtime,
        gap_repair=gap_repair,
    )
    api = _server(
        create_http_app(admin, group, store),
        host=config.host,
        port=config.port,
    )
    token = os.environ.get("ONEBOT_ACCESS_TOKEN", "").strip()
    collector = _server(
        create_collector_app(runtime, token),
        host=COLLECTOR_HOST,
        port=COLLECTOR_PORT,
        log_level="warning",
    )
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(gap_repair.run_forever())
            tasks.create_task(runtime.run_watchdog_forever())
            tasks.create_task(api.serve())
            tasks.create_task(collector.serve())
    finally:
        runtime.end_event_session(
            reason="application_shutdown",
            open_gap=True,
            confidence="suspected",
        )
        await client.close()
