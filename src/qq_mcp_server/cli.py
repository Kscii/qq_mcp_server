from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import NoReturn

from qq_mcp_server.application import build_services, run_server
from qq_mcp_server.config import ConfigError, default_config_text, load_config
from qq_mcp_server.exporter import TextExporter
from qq_mcp_server.napcat import prepare_napcat_config
from qq_mcp_server.store import MessageStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qq_mcp_server",
        description="只读同步一个 QQ 群的文字消息，并提供 TXT 与 MCP 查询。",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(os.environ.get("QQ_MCP_CONFIG", "config.toml")),
        help="配置文件路径（默认：./config.toml）",
    )
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="交互创建最小配置文件")
    commands.add_parser("sync", help="执行一次同步；首次默认导入全部可获取历史")
    commands.add_parser("run", help="持续同步并启动 MCP 服务")
    status = commands.add_parser("status", help="查看本地归档和同步状态")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    commands.add_parser("export", help="从 SQLite 原子重建纯文本文件")
    napcat = commands.add_parser("prepare-napcat", help="生成安全的本机 NapCat 配置")
    napcat.add_argument("directory", type=Path, help="NapCat 配置目录")
    return parser


def _die(message: str, code: int = 2) -> NoReturn:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(code)


def _setup(path: Path) -> None:
    if path.exists():
        _die(f"配置文件已存在，不会覆盖：{path}")
    print("=== qq_mcp_server 初始化 ===")
    account = input("QQ 账号：").strip()
    group = input("只读目标群号：").strip()
    name = input("群名称（可留空）：").strip()
    if not account.isdigit() or not group.isdigit():
        _die("QQ 账号和群号必须只包含数字")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        default_config_text(account_id=account, group_id=group, group_name=name),
        encoding="utf-8",
    )
    print(f"✓ 已写入 {path}")
    print("下一步设置 ONEBOT_ACCESS_TOKEN，然后运行 qq_mcp_server sync。")


async def _sync_once(path: Path) -> None:
    config = load_config(path)
    client, _, _, service = build_services(config)

    def progress(received: int, inserted: int, pages: int) -> None:
        print(f"\r导入中：{pages} 页，检查 {received} 条，新增 {inserted} 条", end="", flush=True)

    try:
        state = service.store.state(config.group_id)
        result = (
            await service.sync_recent()
            if state["initial_import_complete"]
            else await service.import_all(progress)
        )
        if result.pages:
            print()
        print(
            f"✓ 同步完成：收到 {result.received} 条，"
            f"文字 {result.text_messages} 条，新增 {result.inserted} 条。"
        )
    finally:
        await client.close()


def _status(path: Path, as_json: bool) -> None:
    config = load_config(path)
    state = MessageStore(config.database_path).state(config.group_id)
    payload = {
        "account_id": config.account_id,
        "group_id": config.group_id,
        "group_name": config.group_name,
        "database": str(config.database_path),
        "export": str(config.export_path),
        **state,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"群：{config.group_name}（{config.group_id}）")
    print(f"文字消息：{payload['message_count']} 条")
    print(f"初次历史导入完成：{'是' if payload['initial_import_complete'] else '否'}")
    print(f"最后同步：{payload['last_sync_at'] or '尚未同步'}")
    print(f"最后错误：{payload['last_error'] or '无'}")
    print(f"纯文本：{config.export_path}")


def _export(path: Path) -> None:
    config = load_config(path)
    store = MessageStore(config.database_path)
    TextExporter(
        store,
        group_id=config.group_id,
        group_name=config.group_name,
        path=config.export_path,
        timezone=config.timezone,
    ).write()
    print(f"✓ 已重建 {config.export_path}")


def main() -> None:
    arguments = _parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if arguments.command == "setup":
            _setup(arguments.config)
        elif arguments.command == "sync":
            asyncio.run(_sync_once(arguments.config))
        elif arguments.command == "run":
            asyncio.run(run_server(load_config(arguments.config)))
        elif arguments.command == "status":
            _status(arguments.config, arguments.json)
        elif arguments.command == "export":
            _export(arguments.config)
        elif arguments.command == "prepare-napcat":
            token = os.environ.get("ONEBOT_ACCESS_TOKEN", "").strip()
            prepare_napcat_config(arguments.directory, token)
            print(f"✓ 已准备 NapCat 配置：{arguments.directory}")
    except (ConfigError, ValueError) as error:
        _die(str(error))
