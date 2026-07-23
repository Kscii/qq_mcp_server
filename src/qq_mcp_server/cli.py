from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

from qq_mcp_server.application import build_services, run_server
from qq_mcp_server.config import ConfigError, default_config_text, load_config
from qq_mcp_server.napcat import prepare_napcat_config
from qq_mcp_server.rules import RuleIndex, RuleSource, build_rule_index
from qq_mcp_server.store import MessageStore
from qq_mcp_server.sync import SyncService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多群只读 QQ TRPG MCP 服务")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(os.environ.get("QQ_MCP_CONFIG", "config.toml")),
        help="配置文件路径（默认：./config.toml）",
    )
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="交互创建最小部署配置")
    commands.add_parser("sync", help="对白名单群执行最近同步并继续历史回填")
    commands.add_parser("run", help="持续多群同步并启动 Admin/群 MCP")
    status = commands.add_parser("status", help="查看白名单群、同步和规则索引状态")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    build_rules = commands.add_parser("build-rules", help="离线构建三本规则书的私有只读索引")
    build_rules.add_argument("--investigator", type=Path, required=True, help="调查员手册 PDF")
    build_rules.add_argument("--keeper", type=Path, required=True, help="核心规则书 PDF")
    build_rules.add_argument("--magic", type=Path, required=True, help="魔法大典 PDF")
    build_rules.add_argument("--output", type=Path, help="覆盖 storage.rules 配置路径")
    napcat = commands.add_parser("prepare-napcat", help="生成安全的本机 NapCat 配置")
    napcat.add_argument("directory", type=Path, help="NapCat 配置目录")
    return parser


def _die(message: str, code: int = 2) -> NoReturn:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(code)


def _setup(path: Path) -> None:
    if path.exists():
        _die(f"配置文件已存在，不会覆盖：{path}")
    print("=== qq_mcp_server TRPG 初始化 ===")
    account = input("NapCat 登录 QQ 账号：").strip()
    if not account.isdigit():
        _die("QQ 账号必须只包含数字")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_config_text(account_id=account), encoding="utf-8")
    print(f"✓ 已写入 {path}")
    print("下一步构建规则索引、设置 ONEBOT_ACCESS_TOKEN，然后运行服务并从管理 App 加白名单。")


async def _sync_once(path: Path) -> None:
    config = load_config(path)
    client, store, manager, _, _, _ = build_services(config)
    try:
        targets = store.sync_targets()
        if not targets:
            print("当前没有白名单群；请先运行服务并通过管理 App 打开白名单网页。")
            return
        for target in targets:
            service = SyncService(config, target, client, store, manager.limiter)
            state = store.state(target.group_id)
            if not state["recent_ready"]:
                await service.sync_recent()
            result = (
                await service.sync_recent()
                if store.state(target.group_id)["initial_import_complete"]
                else await service.import_all()
            )
            print(
                f"✓ {target.group_name}（{target.group_id}）："
                f"收到 {result.received}，新增 {result.inserted}，页 {result.pages}"
            )
    finally:
        await client.close()


def _status(path: Path, as_json: bool) -> None:
    config = load_config(path)
    store = MessageStore(config.database_path)
    payload: dict[str, Any] = {
        "account_id": config.account_id,
        "database": str(config.database_path),
        "rules": RuleIndex(config.rules_database_path).health(),
        "groups": [
            {
                **group,
                "sync": store.state(str(group["qq_group_id"])),
                "roles": store.member_roles(str(group["group_key"])),
                "has_character": store.character(str(group["group_key"])) is not None,
            }
            for group in store.list_groups()
        ],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"QQ 账号：{config.account_id}")
    print(f"白名单群：{len(payload['groups'])} 个")
    print(f"规则索引：{'就绪' if payload['rules'].get('ready') else '未就绪'}")
    for group in payload["groups"]:
        sync = group["sync"]
        print(
            f"- {group['qq_group_name']}（{group['qq_group_id']}）："
            f"消息 {sync['message_count']}，最近 {'就绪' if sync['recent_ready'] else '未就绪'}，"
            f"历史 {'完成' if sync['initial_import_complete'] else '回填中'}，"
            f"跑团 {'启用' if group['roleplay_enabled'] else '停用'}"
        )


def _build_rules(arguments: argparse.Namespace) -> None:
    config = load_config(arguments.config)
    output = arguments.output or config.rules_database_path
    result = build_rule_index(
        output,
        [
            RuleSource("investigator", "克苏鲁的呼唤第七版调查员手册", arguments.investigator),
            RuleSource("keeper", "COC7th 核心规则书", arguments.keeper),
            RuleSource("magic", "克苏鲁神话魔法大典", arguments.magic),
        ],
    )
    print(f"✓ 规则索引：{output}；页 {result['page_count']}，块 {result['chunk_count']}")
    for warning in result["warnings"]:
        print(f"警告：{warning}")


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
        elif arguments.command == "build-rules":
            _build_rules(arguments)
        elif arguments.command == "prepare-napcat":
            token = os.environ.get("ONEBOT_ACCESS_TOKEN", "").strip()
            config = load_config(arguments.config)
            prepare_napcat_config(arguments.directory, token, config.account_id)
            print(f"✓ 已准备 NapCat 配置：{arguments.directory}")
    except (ConfigError, ValueError, RuntimeError) as error:
        _die(str(error))
