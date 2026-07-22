from __future__ import annotations

from dataclasses import dataclass


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
