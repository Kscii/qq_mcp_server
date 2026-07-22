from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qq_mcp_server.rules import RuleIndex


def make_rule_index(path: Path) -> RuleIndex:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE rule_chunks (
            chunk_id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL,
            source_title TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            pdf_page INTEGER NOT NULL,
            section TEXT NOT NULL,
            text TEXT NOT NULL,
            search_text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE rule_chunks_fts USING fts5(
            chunk_id UNINDEXED, search_text, tokenize='trigram'
        );
        """
    )
    chunks = [
        (
            "keeper:p0133:c01",
            "keeper",
            "守秘人规则书",
            "a",
            133,
            "第八章 理智",
            "一次损失五点或更多理智时进行智力检定，成功会陷入临时疯狂。",
        ),
        (
            "magic:p0048:c01",
            "magic",
            "魔法书",
            "b",
            48,
            "第四章 法术",
            "这个法术会要求理智检定，失败时可能导致临时疯狂。",
        ),
        (
            "investigator:p0001:c01",
            "investigator",
            "调查员手册",
            "c",
            1,
            "介绍",
            "调查员由玩家扮演并参与模组。",
        ),
    ]
    for chunk in chunks:
        search_text = " ".join((chunk[2], chunk[5], chunk[6])).lower()
        connection.execute(
            "INSERT INTO rule_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*chunk, search_text)
        )
        connection.execute("INSERT INTO rule_chunks_fts VALUES (?, ?)", (chunk[0], search_text))
    connection.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        (("page_count", "3"), ("chunk_count", "3"), ("schema_version", "1")),
    )
    connection.commit()
    connection.close()
    return RuleIndex(path)


def test_rule_search_prefers_the_relevant_chapter(tmp_path: Path) -> None:
    rules = make_rule_index(tmp_path / "rules.sqlite3")
    assert rules.health()["ready"] is True
    result = rules.search("理智检定如何判定临时疯狂", book="all", limit=2)
    assert result[0]["book"] == "keeper"
    assert result[0]["pdf_page"] == 133
    assert "一次损失五点" in result[0]["text"]


def test_rule_search_rejects_unknown_book(tmp_path: Path) -> None:
    rules = make_rule_index(tmp_path / "rules.sqlite3")
    with pytest.raises(ValueError, match="book 必须"):
        rules.search("理智", book="core", limit=2)
