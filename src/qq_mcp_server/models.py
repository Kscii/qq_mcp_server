from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True, slots=True)
class ChatMessage:
    group_id: str
    message_id: str
    message_seq: str
    sent_at: int
    sender_id: str
    sender_nickname: str
    sender_card: str
    sender_display: str
    plain_text: str
    reply_to_message_id: str | None
    contains_unsupported_media: bool


@dataclass(frozen=True, slots=True)
class SyncResult:
    received: int
    text_messages: int
    inserted: int
    pages: int
    complete: bool
    boundary_found: bool


@dataclass(frozen=True, slots=True)
class GroupTarget:
    group_key: str
    group_id: str
    group_name: str


class CardOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["set", "increment", "add", "remove"]
    path: str = Field(min_length=1, max_length=300)
    value: Any = None
    source_message_ids: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_value(self) -> CardOperation:
        if not self.path.startswith("/"):
            raise ValueError("path 必须是以 / 开头的 JSON Pointer")
        if self.op in {"set", "increment", "add"} and self.value is None:
            raise ValueError(f"{self.op} 操作必须提供 value")
        return self


class NoteOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["create", "update", "resolve", "delete"]
    note_id: str | None = Field(default=None, max_length=80)
    category: Literal["npc", "clue", "location", "objective", "event", "other"] | None = None
    title: str | None = Field(default=None, max_length=120)
    content: str | None = Field(default=None, max_length=4000)
    source_message_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_fields(self) -> NoteOperation:
        if self.op == "create" and (self.category is None or not self.title or not self.content):
            raise ValueError("create 笔记必须提供 category、title 和 content")
        if self.op != "create" and not self.note_id:
            raise ValueError(f"{self.op} 笔记必须提供 note_id")
        if self.op == "update" and not any((self.category, self.title, self.content)):
            raise ValueError("update 笔记至少修改一个字段")
        return self
