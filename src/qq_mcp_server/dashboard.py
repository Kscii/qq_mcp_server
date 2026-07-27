from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse

from qq_mcp_server.config import AppConfig
from qq_mcp_server.models import ROLEPLAY_GUIDANCE_MAX_LENGTH
from qq_mcp_server.runtime import sync_freshness
from qq_mcp_server.store import MessageStore

_SECTIONS = ("overview", "guidance", "card", "notes", "messages", "changes")
_MESSAGE_PAGE_SIZE = 50
_CHANGE_PAGE_SIZE = 20
_QUERY_MAX_LENGTH = 200
_MISSING = object()
_CARD_METADATA_KEYS = {"schema_version", "template_id", "character_id", "provenance"}
_CARD_LABELS = {
    "identity": "人物信息",
    "era_time": "时代与时间",
    "attributes": "属性",
    "vitals": "生命与状态",
    "skills": "技能",
    "weapons": "武器",
    "assets": "资产",
    "background": "背景",
    "inventory": "物品",
    "experiences": "经历",
    "myth_contacts": "神话接触",
    "schema_version": "数据版本",
    "template_id": "模板",
    "character_id": "人物标识",
    "provenance": "表格来源",
}
_ROLE_LABELS = {
    "player": "玩家",
    "kp": "KP",
    "dice_bot": "骰娘",
    "other_pl": "其他成员",
}
_NOTE_LABELS = {
    "npc": "人物",
    "clue": "线索",
    "location": "地点",
    "objective": "目标",
    "event": "事件",
    "other": "其他",
}

_ENVIRONMENT = Environment(
    loader=PackageLoader("qq_mcp_server", "templates"),
    autoescape=select_autoescape(("html", "xml")),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _format_time(value: object, timezone: ZoneInfo) -> str | None:
    if value in {None, ""}:
        return None
    try:
        if isinstance(value, int):
            moment = datetime.fromtimestamp(value, UTC)
        else:
            moment = datetime.fromisoformat(str(value))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(timezone).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return str(value)


def _parse_filter_time(value: str, timezone: ZoneInfo, field: str) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} 必须是有效日期时间") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone)
    return int(moment.timestamp())


def _display_value(value: object) -> str:
    if value is _MISSING:
        return "（不存在）"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _card_differences(base: object, current: object, path: str = "") -> list[dict[str, str]]:
    if isinstance(base, dict) and isinstance(current, dict):
        differences: list[dict[str, str]] = []
        for key in sorted(set(base) | set(current)):
            if not path and key in _CARD_METADATA_KEYS:
                continue
            child_path = f"{path}/{key}" if path else str(key)
            differences.extend(
                _card_differences(
                    base.get(key, _MISSING),
                    current.get(key, _MISSING),
                    child_path,
                )
            )
        return differences
    if base == current:
        return []
    return [
        {
            "path": path,
            "before": _display_value(base),
            "after": _display_value(current),
        }
    ]


def _role_rows(roles: dict[str, Any]) -> list[dict[str, str]]:
    names = roles["display_names"]
    rows: list[dict[str, str]] = []
    groups = [
        ("player", [roles["player_qq_user_id"]] if roles["player_qq_user_id"] else []),
        ("kp", roles["kp_qq_user_ids"]),
        ("dice_bot", roles["dice_bot_qq_user_ids"]),
    ]
    for role, user_ids in groups:
        for user_id in user_ids:
            value = str(user_id)
            rows.append(
                {
                    "role": _ROLE_LABELS[role],
                    "user_id": value,
                    "display_name": str(names.get(value) or value),
                }
            )
    return rows


def _message_role(sender_id: str, roles: dict[str, Any]) -> str:
    if sender_id == roles["player_qq_user_id"]:
        return "player"
    if sender_id in roles["kp_qq_user_ids"]:
        return "kp"
    if sender_id in roles["dice_bot_qq_user_ids"]:
        return "dice_bot"
    return "other_pl"


def _base_context(
    *,
    config: AppConfig,
    store: MessageStore,
    group: dict[str, Any],
    token: str,
    section: str,
) -> dict[str, Any]:
    character = store.character(str(group["group_key"]))
    character_name = (
        str(character["current"].get("identity", {}).get("name") or "") if character else ""
    )
    return {
        "token": token,
        "section": section,
        "sections": [
            ("overview", "概览"),
            ("guidance", "RP 准则"),
            ("card", "人物卡"),
            ("notes", "团务笔记"),
            ("messages", "群消息"),
            ("changes", "变更记录"),
        ],
        "group": group,
        "page_title": str(
            group["display_label"]
            or group["module_title"]
            or character_name
            or group["qq_group_name"]
        ),
        "character_name": character_name or None,
        "timezone_name": config.timezone,
    }


def _overview_context(
    config: AppConfig, store: MessageStore, group: dict[str, Any], timezone: ZoneInfo
) -> dict[str, Any]:
    group_key = str(group["group_key"])
    group_id = str(group["qq_group_id"])
    sync = store.state(group_id)
    character = store.character(group_key)
    return {
        "roles": _role_rows(store.member_roles(group_key)),
        "sync": {
            **sync,
            "last_sync_at_display": _format_time(sync["last_sync_at"], timezone),
            "oldest_time_display": _format_time(sync["oldest_time"], timezone),
            "newest_time_display": _format_time(sync["newest_time"], timezone),
        },
        "freshness": sync_freshness(sync, config.context_freshness_seconds),
        "character_source": (
            {
                "filename": character["source_filename"],
                "sha256": character["source_sha256"],
                "imported_at": _format_time(character["imported_at"], timezone),
            }
            if character
            else None
        ),
    }


def _card_context(store: MessageStore, group: dict[str, Any], timezone: ZoneInfo) -> dict[str, Any]:
    character = store.character(str(group["group_key"]))
    if not character:
        return {"character": None}
    current = character["current"]
    return {
        "character": {
            "source_filename": character["source_filename"],
            "source_sha256": character["source_sha256"],
            "imported_at": _format_time(character["imported_at"], timezone),
            "sections": [
                {
                    "key": key,
                    "label": _CARD_LABELS.get(key, key),
                    "value": value,
                }
                for key, value in current.items()
                if key not in _CARD_METADATA_KEYS
            ],
            "differences": _card_differences(character["base"], current),
            "full_json": json.dumps(current, ensure_ascii=False, indent=2),
        }
    }


def _notes_context(
    store: MessageStore, group: dict[str, Any], timezone: ZoneInfo
) -> dict[str, Any]:
    notes = store.notes(str(group["group_key"]), include_resolved=True)
    for note in notes:
        note["category_label"] = _NOTE_LABELS.get(str(note["category"]), str(note["category"]))
        note["created_at_display"] = _format_time(note["created_at"], timezone)
        note["updated_at_display"] = _format_time(note["updated_at"], timezone)
    return {"notes": notes}


def _message_context(
    request: Request,
    store: MessageStore,
    group: dict[str, Any],
    timezone: ZoneInfo,
    token: str,
) -> dict[str, Any]:
    query = str(request.query_params.get("query") or "").strip()
    sender = str(request.query_params.get("sender") or "").strip()
    after = str(request.query_params.get("after") or "").strip()
    before = str(request.query_params.get("before") or "").strip()
    cursor = str(request.query_params.get("before_message_id") or "").strip()
    if len(query) > _QUERY_MAX_LENGTH:
        raise ValueError(f"消息检索词不能超过 {_QUERY_MAX_LENGTH} 字")
    if sender and not sender.isdigit():
        raise ValueError("发送者 QQ 号只能包含数字")
    group_id = str(group["qq_group_id"])
    filters_used = bool(query or sender or after or before)
    if filters_used:
        rows = store.search(
            group_id,
            query=query or None,
            sender_id=sender or None,
            start_timestamp=_parse_filter_time(after, timezone, "起始时间"),
            end_timestamp=_parse_filter_time(before, timezone, "结束时间"),
            limit=_MESSAGE_PAGE_SIZE + 1,
            before_message_id=cursor or None,
        )
    else:
        rows = store.recent(
            group_id,
            limit=_MESSAGE_PAGE_SIZE + 1,
            before_message_id=cursor or None,
        )
    has_more = len(rows) > _MESSAGE_PAGE_SIZE
    messages = rows[-_MESSAGE_PAGE_SIZE:]
    roles = store.member_roles(str(group["group_key"]))
    for message in messages:
        role = _message_role(str(message["sender_id"]), roles)
        message["sender_role"] = role
        message["sender_role_label"] = _ROLE_LABELS[role]
        message["sent_at_display"] = _format_time(message["sent_at"], timezone)
    older_url = None
    if has_more and messages:
        parameters = {
            "section": "messages",
            "query": query,
            "sender": sender,
            "after": after,
            "before": before,
            "before_message_id": str(messages[0]["message_id"]),
        }
        older_url = f"/dashboard/{token}?{urlencode(parameters)}"
    return {
        "messages": messages,
        "message_filters": {
            "query": query,
            "sender": sender,
            "after": after,
            "before": before,
        },
        "messages_filtered": filters_used,
        "older_messages_url": older_url,
    }


def _change_context(
    request: Request,
    store: MessageStore,
    group: dict[str, Any],
    timezone: ZoneInfo,
    token: str,
) -> dict[str, Any]:
    cursor = str(request.query_params.get("before_change_id") or "").strip()
    rows = store.list_changes(
        str(group["group_key"]),
        limit=_CHANGE_PAGE_SIZE + 1,
        before_change_id=cursor or None,
    )
    has_more = len(rows) > _CHANGE_PAGE_SIZE
    changes = rows[:_CHANGE_PAGE_SIZE]
    for change in changes:
        change["created_at_display"] = _format_time(change["created_at"], timezone)
        change["operations_json"] = json.dumps(change["operations"], ensure_ascii=False, indent=2)
    older_url = None
    if has_more and changes:
        older_url = f"/dashboard/{token}?" + urlencode(
            {
                "section": "changes",
                "before_change_id": str(changes[-1]["change_id"]),
            }
        )
    return {"changes": changes, "older_changes_url": older_url}


def campaign_dashboard_response(
    request: Request,
    *,
    config: AppConfig,
    store: MessageStore,
    capability: dict[str, Any],
    token: str,
) -> HTMLResponse:
    section = str(request.query_params.get("section") or "overview")
    if section not in _SECTIONS:
        raise ValueError("未知的模组面板页面")
    group_key = str(capability.get("group_key") or "")
    if not group_key:
        raise ValueError("模组面板链接没有绑定群")
    group = store.get_group(group_key)
    timezone = ZoneInfo(config.timezone)
    context = _base_context(
        config=config,
        store=store,
        group=group,
        token=token,
        section=section,
    )
    context["refresh_url"] = request.url.path + (
        f"?{request.url.query}" if request.url.query else ""
    )
    if section == "overview":
        context.update(_overview_context(config, store, group, timezone))
    elif section == "guidance":
        context.update(
            {
                "roleplay_guidance": group["roleplay_guidance"],
                "guidance_length": len(str(group["roleplay_guidance"])),
                "guidance_limit": ROLEPLAY_GUIDANCE_MAX_LENGTH,
            }
        )
    elif section == "card":
        context.update(_card_context(store, group, timezone))
    elif section == "notes":
        context.update(_notes_context(store, group, timezone))
    elif section == "messages":
        context.update(_message_context(request, store, group, timezone, token))
    elif section == "changes":
        context.update(_change_context(request, store, group, timezone, token))

    body = _ENVIRONMENT.get_template("dashboard.html").render(**context)
    return HTMLResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
