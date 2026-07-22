from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qq_mcp_server.models import ChatMessage


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class MessageStore:
    """单文件 SQLite 存储；每个公开方法独立获取短连接。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_seq TEXT NOT NULL,
                    sent_at INTEGER NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_nickname TEXT NOT NULL,
                    sender_card TEXT NOT NULL,
                    sender_display TEXT NOT NULL,
                    plain_text TEXT NOT NULL,
                    reply_to_message_id TEXT,
                    contains_unsupported_media INTEGER NOT NULL DEFAULT 0,
                    first_imported_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL,
                    UNIQUE (group_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_group_time
                    ON messages (group_id, sent_at, id);
                CREATE INDEX IF NOT EXISTS idx_messages_group_sender
                    ON messages (group_id, sender_id, sent_at);

                CREATE TABLE IF NOT EXISTS sync_state (
                    group_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    latest_message_id TEXT,
                    oldest_message_seq TEXT,
                    initial_import_complete INTEGER NOT NULL DEFAULT 0,
                    last_sync_at TEXT,
                    last_error TEXT
                );
                """
            )
            connection.commit()
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def upsert(self, messages: Iterable[ChatMessage]) -> tuple[int, int]:
        batch = list(messages)
        if not batch:
            return 0, 0
        now = _utc_now()
        group_id = batch[0].group_id
        ids = [message.message_id for message in batch]
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as connection:
            existing = {
                str(row[0])
                for row in connection.execute(
                    f"""SELECT message_id FROM messages
                         WHERE group_id = ? AND message_id IN ({placeholders})""",
                    (group_id, *ids),
                )
            }
            with connection:
                connection.executemany(
                    """
                    INSERT INTO messages (
                        group_id, message_id, message_seq, sent_at, sender_id,
                        sender_nickname, sender_card, sender_display, plain_text,
                        reply_to_message_id, contains_unsupported_media,
                        first_imported_at, last_confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (group_id, message_id) DO UPDATE SET
                        message_seq = excluded.message_seq,
                        sent_at = excluded.sent_at,
                        sender_id = excluded.sender_id,
                        sender_nickname = excluded.sender_nickname,
                        sender_card = excluded.sender_card,
                        sender_display = excluded.sender_display,
                        plain_text = excluded.plain_text,
                        reply_to_message_id = excluded.reply_to_message_id,
                        contains_unsupported_media = excluded.contains_unsupported_media,
                        last_confirmed_at = excluded.last_confirmed_at
                    """,
                    [
                        (
                            message.group_id,
                            message.message_id,
                            message.message_seq,
                            message.sent_at,
                            message.sender_id,
                            message.sender_nickname,
                            message.sender_card,
                            message.sender_display,
                            message.plain_text,
                            message.reply_to_message_id,
                            int(message.contains_unsupported_media),
                            now,
                            now,
                        )
                        for message in batch
                    ],
                )
        return len(batch), sum(message.message_id not in existing for message in batch)

    def state(self, group_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT state.account_id, state.latest_message_id,
                       state.oldest_message_seq, state.initial_import_complete,
                       state.last_sync_at, state.last_error,
                       count(message.id) AS message_count,
                       min(message.sent_at) AS oldest_time,
                       max(message.sent_at) AS newest_time
                FROM (SELECT ? AS group_id) AS target
                LEFT JOIN sync_state AS state ON state.group_id = target.group_id
                LEFT JOIN messages AS message ON message.group_id = target.group_id
                GROUP BY target.group_id
                """,
                (group_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def update_state(
        self,
        *,
        account_id: str,
        group_id: str,
        latest_message_id: str | None = None,
        oldest_message_seq: str | None = None,
        initial_import_complete: bool | None = None,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO sync_state (
                    group_id, account_id, latest_message_id, oldest_message_seq,
                    initial_import_complete, last_sync_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (group_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    latest_message_id = COALESCE(
                        excluded.latest_message_id, sync_state.latest_message_id
                    ),
                    oldest_message_seq = COALESCE(
                        excluded.oldest_message_seq, sync_state.oldest_message_seq
                    ),
                    initial_import_complete = CASE
                        WHEN ? IS NULL THEN sync_state.initial_import_complete
                        ELSE excluded.initial_import_complete
                    END,
                    last_sync_at = excluded.last_sync_at,
                    last_error = excluded.last_error
                """,
                (
                    group_id,
                    account_id,
                    latest_message_id,
                    oldest_message_seq,
                    int(bool(initial_import_complete)),
                    now,
                    error,
                    initial_import_complete,
                ),
            )

    def record_error(self, *, account_id: str, group_id: str, error: str) -> None:
        self.update_state(account_id=account_id, group_id=group_id, error=error[:500])

    def message_exists(self, group_id: str, message_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM messages WHERE group_id = ? AND message_id = ?",
                (group_id, message_id),
            ).fetchone()
        return row is not None

    def latest_message_id(self, group_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT message_id FROM messages WHERE group_id = ?
                   ORDER BY sent_at DESC, id DESC LIMIT 1""",
                (group_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def oldest_message_seq(self, group_id: str) -> str | None:
        state = self.state(group_id)
        if state["oldest_message_seq"]:
            return str(state["oldest_message_seq"])
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT message_seq FROM messages WHERE group_id = ?
                   ORDER BY sent_at ASC, id ASC LIMIT 1""",
                (group_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def all_messages(self, group_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT message_id, sent_at, sender_id, sender_display, plain_text,
                          reply_to_message_id, contains_unsupported_media
                   FROM messages WHERE group_id = ?
                   ORDER BY sent_at ASC, id ASC""",
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent(
        self, group_id: str, *, limit: int, before_message_id: str | None = None
    ) -> list[dict[str, Any]]:
        before_clause = ""
        parameters: list[object] = [group_id]
        if before_message_id:
            before_clause = """
                AND (sent_at, id) < (
                    SELECT sent_at, id FROM messages
                    WHERE group_id = ? AND message_id = ?
                )
            """
            parameters.extend([group_id, before_message_id])
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT message_id, sent_at, sender_id, sender_display, plain_text,
                            reply_to_message_id
                     FROM messages WHERE group_id = ? {before_clause}
                     ORDER BY sent_at DESC, id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def search(
        self,
        group_id: str,
        *,
        query: str | None,
        sender_id: str | None,
        start_timestamp: int | None,
        end_timestamp: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = ["group_id = ?"]
        parameters: list[object] = [group_id]
        if query:
            clauses.append("plain_text LIKE ? ESCAPE '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{escaped}%")
        if sender_id:
            clauses.append("sender_id = ?")
            parameters.append(sender_id)
        if start_timestamp is not None:
            clauses.append("sent_at >= ?")
            parameters.append(start_timestamp)
        if end_timestamp is not None:
            clauses.append("sent_at <= ?")
            parameters.append(end_timestamp)
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT message_id, sent_at, sender_id, sender_display, plain_text,
                            reply_to_message_id
                     FROM messages WHERE {" AND ".join(clauses)}
                     ORDER BY sent_at ASC, id ASC LIMIT ?""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]
