from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qq_mcp_server.store import MessageStore


class TextExporter:
    def __init__(
        self,
        store: MessageStore,
        *,
        group_id: str,
        group_name: str,
        path: Path,
        timezone: str,
    ) -> None:
        self.store = store
        self.group_id = group_id
        self.group_name = group_name
        self.path = path
        try:
            self.timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"未知时区：{timezone}") from error

    def render(self) -> str:
        lines = [
            f"# QQ 群文字归档：{self.group_name}",
            f"# 群号：{self.group_id}",
            "# 仅包含文字；发送人名称为消息当时的群名片或昵称。",
            "",
        ]
        for message in self.store.all_messages(self.group_id):
            timestamp = datetime.fromtimestamp(message["sent_at"], self.timezone)
            lines.append(
                f"[{timestamp:%Y-%m-%d %H:%M:%S %z}] "
                f"{message['sender_display']}（QQ {message['sender_id']}）"
            )
            if message["reply_to_message_id"]:
                lines.append(f"[回复消息 {message['reply_to_message_id']}]")
            lines.append(str(message["plain_text"]))
            lines.append("")
        return "\n".join(lines)

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(self.render(), encoding="utf-8", newline="\n")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
