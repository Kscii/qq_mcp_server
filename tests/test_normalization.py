from __future__ import annotations

import pytest

from qq_mcp_server.normalization import normalize_message


def test_keeps_text_sender_reply_and_drops_media_url() -> None:
    raw = {
        "group_id": 2,
        "message_id": 10,
        "message_seq": 11,
        "time": 1_700_000_000,
        "user_id": 3,
        "sender": {"nickname": "昵称", "card": "群名片"},
        "message": [
            {"type": "reply", "data": {"id": "9"}},
            {"type": "at", "data": {"qq": "4", "name": "KP"}},
            {"type": "text", "data": {"text": " 内容"}},
            {"type": "image", "data": {"url": "https://secret.invalid/image"}},
        ],
    }
    message = normalize_message(raw, expected_group_id="2")
    assert message is not None
    assert message.sender_id == "3"
    assert message.sender_display == "群名片"
    assert message.plain_text == "@KP 内容"
    assert message.reply_to_message_id == "9"
    assert message.contains_unsupported_media is True
    assert "secret.invalid" not in repr(message)


def test_discards_media_only_message() -> None:
    assert (
        normalize_message(
            {
                "group_id": 2,
                "message_id": 10,
                "user_id": 3,
                "message": [{"type": "image", "data": {"url": "secret"}}],
            },
            expected_group_id="2",
        )
        is None
    )


def test_rejects_other_group() -> None:
    with pytest.raises(ValueError, match="非目标群"):
        normalize_message(
            {
                "group_id": 999,
                "message_id": 10,
                "user_id": 3,
                "message": [{"type": "text", "data": {"text": "no"}}],
            },
            expected_group_id="2",
        )
