from __future__ import annotations

import re
from typing import Any

from qq_mcp_server.models import ChatMessage

_CQ_SEGMENT = re.compile(r"\[CQ:[^\]]+\]")


def normalize_message(raw: dict[str, Any], *, expected_group_id: str) -> ChatMessage | None:
    group_id = str(raw.get("group_id") or expected_group_id)
    if group_id != expected_group_id:
        raise ValueError(f"OneBot 返回了非目标群消息：{group_id}")

    segments = raw.get("message")
    text_parts: list[str] = []
    reply_to: str | None = None
    unsupported = False
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "")
            raw_data = segment.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            if segment_type == "text":
                text = data.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif segment_type == "at":
                user_id = str(data.get("qq") or data.get("user_id") or "")
                if user_id:
                    text_parts.append(f"@{data.get('name') or user_id}")
            elif segment_type == "reply" and reply_to is None:
                reply_to = str(data.get("id") or data.get("message_id") or "") or None
            elif segment_type:
                unsupported = True
    else:
        fallback = raw.get("raw_message")
        if isinstance(fallback, str):
            cleaned = _CQ_SEGMENT.sub("", fallback)
            text_parts.append(cleaned)
            unsupported = cleaned != fallback

    plain_text = "".join(text_parts)
    if not plain_text.strip():
        if unsupported:
            plain_text = "[未读取的媒体消息]"
        else:
            return None

    raw_sender = raw.get("sender")
    sender: dict[str, Any] = raw_sender if isinstance(raw_sender, dict) else {}
    sender_id = str(raw.get("user_id") or sender.get("user_id") or "")
    message_id = str(raw.get("message_id") or "")
    if not sender_id or not message_id:
        raise ValueError("消息缺少发送人 QQ 号或消息 ID")
    nickname = str(sender.get("nickname") or "")
    card = str(sender.get("card") or "")
    return ChatMessage(
        group_id=group_id,
        message_id=message_id,
        message_seq=str(raw.get("message_seq") or message_id),
        sent_at=int(raw.get("time") or 0),
        sender_id=sender_id,
        sender_nickname=nickname,
        sender_card=card,
        sender_display=card or nickname or sender_id,
        plain_text=plain_text,
        reply_to_message_id=reply_to,
        contains_unsupported_media=unsupported,
    )


def oldest_cursor(raw_messages: list[dict[str, Any]]) -> str | None:
    if not raw_messages:
        return None

    def sequence(raw: dict[str, Any]) -> int:
        try:
            return int(raw.get("message_seq") or raw.get("message_id") or 0)
        except (TypeError, ValueError):
            return 0

    oldest = min(
        raw_messages,
        key=lambda raw: (int(raw.get("time") or 0), sequence(raw)),
    )
    return str(oldest.get("message_seq") or oldest.get("message_id") or "") or None
