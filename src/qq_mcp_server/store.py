from __future__ import annotations

import copy
import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from qq_mcp_server.models import (
    ROLEPLAY_GUIDANCE_MAX_LENGTH,
    CardOperation,
    ChatMessage,
    GroupTarget,
    NoteOperation,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _unjson(value: object, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return copy.deepcopy(fallback)
    return json.loads(value)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def backup_database(path: Path, directory: Path) -> Path:
    source_path = path.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{source_path}")
    target_directory = directory.expanduser().resolve()
    target_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_directory / f"{source_path.stem}.pre-v4.{timestamp}.sqlite3"
    with (
        closing(sqlite3.connect(source_path)) as source,
        closing(sqlite3.connect(target)) as destination,
    ):
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError("数据库备份完整性检查失败")
    target.chmod(0o600)
    return target


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer 必须以 / 开头")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _pointer_get(document: Any, pointer: str) -> tuple[bool, Any]:
    current = document
    try:
        for part in _pointer_parts(pointer):
            current = current[int(part)] if isinstance(current, list) else current[part]
    except (KeyError, IndexError, TypeError, ValueError):
        return False, None
    return True, copy.deepcopy(current)


def _pointer_parent(document: Any, pointer: str) -> tuple[Any, str]:
    parts = _pointer_parts(pointer)
    if not parts or parts == [""]:
        raise ValueError("不允许修改整张人物卡根节点")
    current = document
    for part in parts[:-1]:
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ValueError(f"人物卡路径不存在：{pointer}") from error
    return current, parts[-1]


def _pointer_set(document: Any, pointer: str, value: Any) -> None:
    parent, key = _pointer_parent(document, pointer)
    if isinstance(parent, list):
        try:
            parent[int(key)] = copy.deepcopy(value)
        except (IndexError, ValueError) as error:
            raise ValueError(f"人物卡列表路径不存在：{pointer}") from error
    elif isinstance(parent, dict):
        parent[key] = copy.deepcopy(value)
    else:
        raise ValueError(f"人物卡路径不是可写容器：{pointer}")


def _pointer_remove(document: Any, pointer: str) -> None:
    parent, key = _pointer_parent(document, pointer)
    if isinstance(parent, list):
        try:
            del parent[int(key)]
        except (IndexError, ValueError) as error:
            raise ValueError(f"人物卡列表路径不存在：{pointer}") from error
    elif isinstance(parent, dict):
        if key not in parent:
            raise ValueError(f"人物卡路径不存在：{pointer}")
        del parent[key]
    else:
        raise ValueError(f"人物卡路径不是可写容器：{pointer}")


_CARD_TOP_LEVEL = {
    "identity",
    "era_time",
    "attributes",
    "vitals",
    "skills",
    "weapons",
    "assets",
    "background",
    "inventory",
    "experiences",
    "myth_contacts",
}


def _validate_card_path(path: str, origin: str) -> None:
    parts = _pointer_parts(path)
    if not parts or parts[0] not in _CARD_TOP_LEVEL:
        raise ValueError(f"不允许修改人物卡路径：{path}")
    if origin == "qq_event" and parts[0] in {"identity", "era_time"}:
        raise ValueError("群消息事件不能自动修改人物身份或时代")


def _validate_card(document: dict[str, Any]) -> None:
    identity = document.get("identity")
    if not isinstance(identity, dict) or not str(identity.get("name") or "").strip():
        raise ValueError("人物卡必须保留角色姓名")
    attributes = document.get("attributes")
    if not isinstance(attributes, dict):
        raise ValueError("人物卡 attributes 格式错误")
    for key, value in attributes.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"人物属性 {key} 必须是数字")
        if not 0 <= value <= 999:
            raise ValueError(f"人物属性 {key} 超出 0..999")
    skills = document.get("skills", {})
    if not isinstance(skills, dict):
        raise ValueError("人物卡 skills 格式错误")
    for name, skill in skills.items():
        if not isinstance(skill, dict):
            raise ValueError(f"技能 {name} 格式错误")
        regular = skill.get("regular")
        if isinstance(regular, bool) or not isinstance(regular, (int, float)):
            raise ValueError(f"技能 {name} 的普通成功率必须是数字")
        if not 0 <= regular <= 999:
            raise ValueError(f"技能 {name} 超出 0..999")
        skill["hard"] = int(regular) // 2
        skill["extreme"] = int(regular) // 5


class VersionConflictError(RuntimeError):
    def __init__(self, current_version: int) -> None:
        super().__init__(f"数据版本已变化，当前版本为 {current_version}")
        self.current_version = current_version


class MessageStore:
    """TRPG 单文件 SQLite 存储；公开方法使用短连接和显式事务。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as connection:
            existing_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing_tables and "app_metadata" not in existing_tables:
                raise ValueError(
                    "检测到旧版 Dice Echo 或外部 SQLite；此类数据库不执行迁移，"
                    "请配置一个新的 database 路径"
                )
            schema_version: str | None = None
            if "app_metadata" in existing_tables:
                schema = connection.execute(
                    "SELECT value FROM app_metadata WHERE key = 'schema_version'"
                ).fetchone()
                schema_version = str(schema[0]) if schema else None
                if schema_version not in {"2", "3", "4"}:
                    raise ValueError("数据库 schema_version 不受支持；请使用新的 database 路径")
            connection.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO app_metadata(key, value) VALUES ('schema_version', '4');

                CREATE TABLE IF NOT EXISTS groups (
                    group_key TEXT PRIMARY KEY,
                    qq_group_id TEXT NOT NULL UNIQUE,
                    qq_group_name TEXT NOT NULL,
                    module_title TEXT NOT NULL DEFAULT '',
                    display_label TEXT NOT NULL DEFAULT '',
                    roleplay_guidance TEXT NOT NULL DEFAULT '',
                    history_since TEXT,
                    roleplay_enabled INTEGER NOT NULL DEFAULT 0,
                    whitelisted INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_groups_whitelist
                    ON groups (whitelisted, qq_group_id);

                CREATE TABLE IF NOT EXISTS group_member_roles (
                    group_key TEXT NOT NULL REFERENCES groups(group_key) ON DELETE CASCADE,
                    qq_user_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('player', 'kp', 'dice_bot')),
                    display_name TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (group_key, qq_user_id, role)
                );
                CREATE INDEX IF NOT EXISTS idx_group_member_roles_role
                    ON group_member_roles (group_key, role, qq_user_id);

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
                    reconcile_cursor TEXT,
                    reconcile_boundary_id TEXT,
                    reconcile_newest_id TEXT,
                    recent_ready INTEGER NOT NULL DEFAULT 0,
                    initial_import_complete INTEGER NOT NULL DEFAULT 0,
                    last_sync_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS group_candidates (
                    group_id TEXT PRIMARY KEY,
                    group_name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1,
                    verification_status TEXT NOT NULL DEFAULT 'unverified',
                    verification_method TEXT,
                    verified_until TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_group_candidates_seen
                    ON group_candidates (available, last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS runtime_status (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_events_time
                    ON runtime_events (created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS collector_sessions (
                    session_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    connected_at TEXT NOT NULL,
                    last_heartbeat_at TEXT,
                    heartbeat_interval_ms INTEGER,
                    online INTEGER,
                    good INTEGER,
                    ended_at TEXT,
                    end_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_collector_sessions_time
                    ON collector_sessions (connected_at DESC);

                CREATE TABLE IF NOT EXISTS message_gaps (
                    gap_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    start_at INTEGER NOT NULL,
                    end_at INTEGER,
                    confidence TEXT NOT NULL
                        CHECK (confidence IN ('confirmed', 'suspected')),
                    source TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN (
                            'open', 'paused', 'repairing', 'repaired',
                            'unverified', 'accepted'
                        )),
                    before_message_id TEXT,
                    after_message_id TEXT,
                    repair_cursor TEXT,
                    pages_attempted INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    accepted_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_message_gaps_group_time
                    ON message_gaps (group_id, start_at, status);

                CREATE TABLE IF NOT EXISTS onebot_action_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    source TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'error')),
                    latency_ms REAL NOT NULL,
                    error_type TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_onebot_action_audit_time
                    ON onebot_action_audit (created_at DESC, action);

                CREATE TABLE IF NOT EXISTS qq_accounts (
                    account_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('registered', 'pending', 'active', 'disabled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_activated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_qq_account
                    ON qq_accounts (status) WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS qq_account_switches (
                    switch_id TEXT PRIMARY KEY,
                    from_account_id TEXT,
                    target_account_id TEXT NOT NULL
                        REFERENCES qq_accounts(account_id),
                    status TEXT NOT NULL
                        CHECK (status IN (
                            'requested', 'host_pending', 'awaiting_login',
                            'completed', 'failed', 'cancelled'
                        )),
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_qq_account_switches_time
                    ON qq_account_switches (requested_at DESC);

                CREATE TABLE IF NOT EXISTS characters (
                    group_key TEXT PRIMARY KEY REFERENCES groups(group_key) ON DELETE CASCADE,
                    source_filename TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    base_json TEXT NOT NULL,
                    current_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS campaign_notes (
                    note_id TEXT PRIMARY KEY,
                    group_key TEXT NOT NULL REFERENCES groups(group_key) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'resolved')),
                    source_message_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notes_group_status
                    ON campaign_notes (group_key, status, updated_at);

                CREATE TABLE IF NOT EXISTS group_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    change_id TEXT NOT NULL UNIQUE,
                    group_key TEXT NOT NULL REFERENCES groups(group_key) ON DELETE CASCADE,
                    origin TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    new_version INTEGER NOT NULL,
                    operations_json TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    touched_json TEXT NOT NULL,
                    undone_by TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_changes_group_id
                    ON group_changes (group_key, id DESC);

                CREATE TABLE IF NOT EXISTS capability_tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    group_key TEXT,
                    issued_to TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            if schema_version == "2":
                group_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(groups)")
                }
                if "history_since" not in group_columns:
                    connection.execute("ALTER TABLE groups ADD COLUMN history_since TEXT")
                sync_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(sync_state)")
                }
                for column in (
                    "reconcile_cursor",
                    "reconcile_boundary_id",
                    "reconcile_newest_id",
                ):
                    if column not in sync_columns:
                        connection.execute(f"ALTER TABLE sync_state ADD COLUMN {column} TEXT")
                connection.execute(
                    "UPDATE app_metadata SET value = '3' WHERE key = 'schema_version'"
                )
                schema_version = "3"
            if schema_version in {"2", "3"}:
                connection.execute("DROP INDEX IF EXISTS idx_one_player_per_group")
                connection.execute(
                    "UPDATE app_metadata SET value = '4' WHERE key = 'schema_version'"
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

    @staticmethod
    def _group_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["roleplay_enabled"] = bool(result["roleplay_enabled"])
        result["whitelisted"] = bool(result["whitelisted"])
        return result

    def list_groups(self, *, whitelisted_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE whitelisted = 1" if whitelisted_only else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM groups {where} ORDER BY qq_group_name, qq_group_id"
            ).fetchall()
        return [self._group_from_row(row) for row in rows]

    def sync_targets(self) -> list[GroupTarget]:
        return [
            GroupTarget(
                group_key=str(row["group_key"]),
                group_id=str(row["qq_group_id"]),
                group_name=str(row["qq_group_name"]),
                history_since=str(row["history_since"]) if row["history_since"] else None,
            )
            for row in self.list_groups()
        ]

    def get_group(self, group_key: str, *, require_whitelisted: bool = True) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM groups WHERE group_key = ?", (group_key,)
            ).fetchone()
        if row is None or (require_whitelisted and not row["whitelisted"]):
            raise KeyError("群尚未授权 AI 访问")
        return self._group_from_row(row)

    def get_group_by_qq(self, group_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM groups WHERE qq_group_id = ?", (group_id,)
            ).fetchone()
        return self._group_from_row(row) if row else None

    def whitelist_group(self, group_id: str, group_name: str) -> dict[str, Any]:
        if not group_id.isdigit():
            raise ValueError("QQ群号只能包含数字")
        name = group_name.strip() or group_id
        now = _utc_now()
        existing = self.get_group_by_qq(group_id)
        with closing(self._connect()) as connection, connection:
            if existing:
                connection.execute(
                    """UPDATE groups SET whitelisted = 1, qq_group_name = ?, updated_at = ?
                       WHERE qq_group_id = ?""",
                    (name, now, group_id),
                )
                group_key = str(existing["group_key"])
            else:
                group_key = f"g_{secrets.token_urlsafe(9)}"
                connection.execute(
                    """INSERT INTO groups (
                           group_key, qq_group_id, qq_group_name, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (group_key, group_id, name, now, now),
                )
        return self.get_group(group_key)

    def remove_from_whitelist(self, group_key: str) -> None:
        self.get_group(group_key)
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE groups SET whitelisted = 0, roleplay_enabled = 0,
                          version = version + 1, updated_at = ? WHERE group_key = ?""",
                (now, group_key),
            )
            connection.execute(
                "UPDATE capability_tokens SET consumed_at = ? WHERE group_key = ? AND consumed_at IS NULL",
                (now, group_key),
            )

    @staticmethod
    def _expect_version(connection: sqlite3.Connection, group_key: str, expected: int) -> int:
        row = connection.execute(
            "SELECT version FROM groups WHERE group_key = ? AND whitelisted = 1", (group_key,)
        ).fetchone()
        if row is None:
            raise KeyError("群尚未授权 AI 访问")
        current = int(row[0])
        if current != expected:
            raise VersionConflictError(current)
        return current

    def update_group_profile(
        self,
        group_key: str,
        *,
        expected_version: int,
        module_title: str | None,
        display_label: str | None,
        roleplay_guidance: str | None,
        history_since: str | None = None,
    ) -> dict[str, Any]:
        values: dict[str, str | None] = {}
        if module_title is not None:
            values["module_title"] = module_title.strip()
        if display_label is not None:
            values["display_label"] = display_label.strip()
        if roleplay_guidance is not None:
            guidance = roleplay_guidance.strip()
            if len(guidance) > ROLEPLAY_GUIDANCE_MAX_LENGTH:
                raise ValueError(
                    "roleplay_guidance 当前为 "
                    f"{len(guidance)} 字，不能超过 {ROLEPLAY_GUIDANCE_MAX_LENGTH} 字"
                )
            values["roleplay_guidance"] = guidance
        history_changed = history_since is not None
        if history_since is not None:
            normalized = history_since.strip()
            if normalized:
                try:
                    parsed = datetime.fromisoformat(normalized)
                except ValueError as error:
                    raise ValueError("history_since 必须是 ISO 8601 时间") from error
                if parsed.tzinfo is None:
                    raise ValueError("history_since 必须包含时区，例如 +08:00")
                normalized = parsed.isoformat()
                values["history_since"] = normalized
            else:
                values["history_since"] = None
        if not values:
            raise ValueError("至少提供一个要修改的配置")
        with closing(self._connect()) as connection, connection:
            self._expect_version(connection, group_key, expected_version)
            assignments = ", ".join(f"{key} = ?" for key in values)
            connection.execute(
                f"""UPDATE groups SET {assignments}, version = version + 1, updated_at = ?
                       WHERE group_key = ?""",
                (*values.values(), _utc_now(), group_key),
            )
            if history_changed:
                group_id = connection.execute(
                    "SELECT qq_group_id FROM groups WHERE group_key = ?", (group_key,)
                ).fetchone()
                assert group_id is not None
                connection.execute(
                    """UPDATE sync_state SET initial_import_complete = 0
                       WHERE group_id = ?""",
                    (str(group_id[0]),),
                )
        return self.get_group(group_key)

    def member_roles(self, group_key: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT qq_user_id, role, display_name FROM group_member_roles
                   WHERE group_key = ? ORDER BY role, qq_user_id""",
                (group_key,),
            ).fetchall()
        players = [str(row["qq_user_id"]) for row in rows if row["role"] == "player"]
        return {
            "player_qq_user_id": players[0] if players else None,
            "player_qq_user_ids": players,
            "kp_qq_user_ids": [str(row["qq_user_id"]) for row in rows if row["role"] == "kp"],
            "dice_bot_qq_user_ids": [
                str(row["qq_user_id"]) for row in rows if row["role"] == "dice_bot"
            ],
            "display_names": {str(row["qq_user_id"]): str(row["display_name"]) for row in rows},
        }

    def set_member_roles(
        self,
        group_key: str,
        *,
        expected_version: int,
        player_qq_user_id: str,
        player_qq_user_ids: list[str] | None = None,
        kp_qq_user_ids: list[str],
        dice_bot_qq_user_ids: list[str],
        display_names: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        players = list(
            dict.fromkeys(
                [
                    player_qq_user_id,
                    *(player_qq_user_ids or []),
                ]
            )
        )
        ids = [*players, *kp_qq_user_ids, *dice_bot_qq_user_ids]
        if any(not item.isdigit() for item in ids):
            raise ValueError("成员 QQ 号只能包含数字")
        if len(ids) != len(set(ids)):
            raise ValueError("同一个 QQ 成员不能同时承担多个特殊身份")
        names = display_names or {}
        rows = [
            *[(group_key, item, "player", names.get(item, "")) for item in players],
            *[(group_key, item, "kp", names.get(item, "")) for item in kp_qq_user_ids],
            *[(group_key, item, "dice_bot", names.get(item, "")) for item in dice_bot_qq_user_ids],
        ]
        with closing(self._connect()) as connection, connection:
            self._expect_version(connection, group_key, expected_version)
            connection.execute("DELETE FROM group_member_roles WHERE group_key = ?", (group_key,))
            connection.executemany(
                """INSERT INTO group_member_roles
                       (group_key, qq_user_id, role, display_name) VALUES (?, ?, ?, ?)""",
                rows,
            )
            connection.execute(
                """UPDATE groups SET version = version + 1, updated_at = ?
                   WHERE group_key = ?""",
                (_utc_now(), group_key),
            )
        return self.member_roles(group_key)

    def set_group_enabled(
        self, group_key: str, *, expected_version: int, enabled: bool
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection, connection:
            self._expect_version(connection, group_key, expected_version)
            connection.execute(
                """UPDATE groups SET roleplay_enabled = ?, version = version + 1,
                          updated_at = ? WHERE group_key = ?""",
                (int(enabled), _utc_now(), group_key),
            )
        return self.get_group(group_key)

    def update_group_name(self, group_id: str, group_name: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE groups SET qq_group_name = ?, updated_at = ? WHERE qq_group_id = ?",
                (group_name.strip() or group_id, _utc_now(), group_id),
            )

    def upsert_group_candidate(
        self,
        group_id: str,
        group_name: str,
        *,
        source: str,
        available: bool = True,
        verification_status: str | None = None,
        verification_method: str | None = None,
        verified_until: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if not group_id.isdigit():
            raise ValueError("候选群号只能包含数字")
        name = group_name.strip() or group_id
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO group_candidates (
                       group_id, group_name, source, first_seen_at, last_seen_at, available
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (group_id, name, source, now, now, int(available)),
            )
            connection.execute(
                """UPDATE group_candidates SET group_name = ?, source = ?,
                          last_seen_at = ?, available = ?, last_error = ?
                   WHERE group_id = ?""",
                (name, source, now, int(available), error[:500] if error else None, group_id),
            )
            if verification_status is not None:
                connection.execute(
                    """UPDATE group_candidates SET verification_status = ?,
                              verification_method = ?, verified_until = ?
                       WHERE group_id = ?""",
                    (
                        verification_status,
                        verification_method,
                        verified_until,
                        group_id,
                    ),
                )
        result = self.group_candidate(group_id)
        assert result is not None
        return result

    def mark_group_candidate_unavailable(self, group_id: str, *, source: str) -> None:
        existing = self.group_candidate(group_id)
        self.upsert_group_candidate(
            group_id,
            str(existing["group_name"]) if existing else group_id,
            source=source,
            available=False,
            verification_status="unverified",
            verification_method=None,
            verified_until=None,
        )

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["available"] = bool(result["available"])
        verified_until = result.get("verified_until")
        result["verification_valid"] = bool(
            verified_until
            and datetime.fromisoformat(str(verified_until)) > datetime.now(UTC)
            and result["verification_status"] == "verified"
        )
        return result

    def group_candidate(self, group_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM group_candidates WHERE group_id = ?", (group_id,)
            ).fetchone()
        return self._candidate_from_row(row) if row else None

    def list_group_candidates(self, *, available_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE available = 1" if available_only else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM group_candidates {where}
                    ORDER BY last_seen_at DESC, group_id"""
            ).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def set_runtime_status(self, key: str, value: dict[str, Any]) -> None:
        if not key or len(key) > 80:
            raise ValueError("运行状态键格式错误")
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO runtime_status (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT (key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (key, _json(value), now),
            )

    def runtime_status(self, key: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value_json, updated_at FROM runtime_status WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return {"updated_at": None}
        value = _unjson(row["value_json"], {})
        if not isinstance(value, dict):
            value = {}
        return {**value, "updated_at": str(row["updated_at"])}

    def record_runtime_event(self, event_type: str, details: dict[str, Any]) -> None:
        if not event_type or len(event_type) > 80:
            raise ValueError("运行事件类型格式错误")
        cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO runtime_events (event_type, details_json, created_at)
                   VALUES (?, ?, ?)""",
                (event_type, _json(details), _utc_now()),
            )
            connection.execute("DELETE FROM runtime_events WHERE created_at < ?", (cutoff,))

    def runtime_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, event_type, details_json, created_at
                   FROM runtime_events ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "event_type": str(row["event_type"]),
                "details": _unjson(row["details_json"], {}),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def start_collector_session(self, account_id: str) -> dict[str, Any]:
        if not account_id.isdigit():
            raise ValueError("采集账号必须是 QQ 号")
        session_id = f"ses_{secrets.token_urlsafe(12)}"
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO collector_sessions (
                       session_id, account_id, connected_at
                   ) VALUES (?, ?, ?)""",
                (session_id, account_id, now),
            )
        return self.collector_session(session_id)

    def collector_session(self, session_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM collector_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError("采集会话不存在")
        result = dict(row)
        result["online"] = None if result["online"] is None else bool(result["online"])
        result["good"] = None if result["good"] is None else bool(result["good"])
        return result

    def active_collector_session(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM collector_sessions WHERE ended_at IS NULL
                   ORDER BY connected_at DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        return self.collector_session(str(row["session_id"]))

    def update_collector_heartbeat(
        self,
        session_id: str,
        *,
        interval_ms: int | None,
        online: bool | None,
        good: bool | None,
    ) -> dict[str, Any]:
        if interval_ms is not None and not 1000 <= interval_ms <= 3_600_000:
            interval_ms = None
        with closing(self._connect()) as connection, connection:
            result = connection.execute(
                """UPDATE collector_sessions
                   SET last_heartbeat_at = ?, heartbeat_interval_ms = ?,
                       online = ?, good = ?
                   WHERE session_id = ? AND ended_at IS NULL""",
                (
                    _utc_now(),
                    interval_ms,
                    None if online is None else int(online),
                    None if good is None else int(good),
                    session_id,
                ),
            )
            if result.rowcount != 1:
                raise KeyError("当前采集会话不存在或已经结束")
        return self.collector_session(session_id)

    def end_collector_session(self, session_id: str, *, reason: str) -> dict[str, Any]:
        text = reason.strip()[:500] or "unknown"
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE collector_sessions SET ended_at = COALESCE(ended_at, ?),
                          end_reason = COALESCE(end_reason, ?)
                   WHERE session_id = ?""",
                (_utc_now(), text, session_id),
            )
        return self.collector_session(session_id)

    def close_abandoned_collector_session(self) -> dict[str, Any] | None:
        active = self.active_collector_session()
        if active is None:
            return None
        self.end_collector_session(str(active["session_id"]), reason="unclean_restart")
        return active

    def collected_group_ids(self) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT group_id FROM group_candidates
                   UNION
                   SELECT group_id FROM messages
                   ORDER BY group_id"""
            ).fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    def _gap_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["pages_attempted"] = int(result["pages_attempted"])
        return result

    @staticmethod
    def _message_boundary(
        connection: sqlite3.Connection,
        group_id: str,
        timestamp: int,
        *,
        before: bool,
    ) -> str | None:
        operator = "<=" if before else ">="
        order = "DESC" if before else "ASC"
        row = connection.execute(
            f"""SELECT message_id FROM messages
                WHERE group_id = ? AND sent_at {operator} ?
                ORDER BY sent_at {order}, id {order} LIMIT 1""",
            (group_id, timestamp),
        ).fetchone()
        return str(row[0]) if row else None

    def create_message_gap(
        self,
        group_id: str,
        *,
        start_at: int,
        end_at: int | None,
        confidence: str,
        source: str,
    ) -> dict[str, Any]:
        if not group_id.isdigit():
            raise ValueError("缺口群号只能包含数字")
        if confidence not in {"confirmed", "suspected"}:
            raise ValueError("confidence 必须是 confirmed 或 suspected")
        if start_at <= 0 or (end_at is not None and end_at < start_at):
            raise ValueError("缺口时间范围无效")
        source = source.strip()[:80]
        if not source:
            raise ValueError("缺口来源不能为空")
        now = _utc_now()
        gap_id: str
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """SELECT * FROM message_gaps
                   WHERE group_id = ? AND end_at IS NULL
                     AND status IN ('open', 'paused', 'repairing')
                   ORDER BY start_at LIMIT 1""",
                (group_id,),
            ).fetchone()
            if existing is not None:
                if confidence == "confirmed" and existing["confidence"] == "suspected":
                    connection.execute(
                        """UPDATE message_gaps SET confidence = 'confirmed',
                                  source = ?, updated_at = ? WHERE gap_id = ?""",
                        (source, now, existing["gap_id"]),
                    )
                gap_id = str(existing["gap_id"])
            else:
                before_id = self._message_boundary(connection, group_id, start_at, before=True)
                after_id = (
                    self._message_boundary(connection, group_id, end_at, before=False)
                    if end_at is not None
                    else None
                )
                gap_id = f"gap_{secrets.token_urlsafe(12)}"
                connection.execute(
                    """INSERT INTO message_gaps (
                           gap_id, group_id, start_at, end_at, confidence, source,
                           status, before_message_id, after_message_id, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
                    (
                        gap_id,
                        group_id,
                        start_at,
                        end_at,
                        confidence,
                        source,
                        before_id,
                        after_id,
                        now,
                        now,
                    ),
                )
        return self.message_gap(gap_id)

    def create_message_gaps_for_all(
        self,
        *,
        start_at: int,
        confidence: str,
        source: str,
    ) -> list[dict[str, Any]]:
        return [
            self.create_message_gap(
                group_id,
                start_at=start_at,
                end_at=None,
                confidence=confidence,
                source=source,
            )
            for group_id in self.collected_group_ids()
        ]

    def close_open_message_gaps(
        self, *, end_at: int, automatic_only: bool = True
    ) -> list[dict[str, Any]]:
        source_clause = (
            " AND source IN "
            "('sse_disconnect', 'heartbeat_degraded', 'heartbeat_timeout', "
            "'unclean_restart', 'account_switch', 'collection_pause')"
            if automatic_only
            else ""
        )
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """SELECT gap_id, group_id FROM message_gaps
                   WHERE end_at IS NULL AND status IN ('open', 'paused')"""
                + source_clause
            ).fetchall()
            for row in rows:
                after_id = self._message_boundary(
                    connection, str(row["group_id"]), end_at, before=False
                )
                connection.execute(
                    """UPDATE message_gaps SET end_at = ?, after_message_id = ?,
                              updated_at = ? WHERE gap_id = ?""",
                    (end_at, after_id, _utc_now(), row["gap_id"]),
                )
        return [self.message_gap(str(row["gap_id"])) for row in rows]

    def refresh_message_gap_boundaries(self, gap_id: str) -> dict[str, Any]:
        gap = self.message_gap(gap_id)
        with closing(self._connect()) as connection, connection:
            before_id = gap["before_message_id"] or self._message_boundary(
                connection, str(gap["group_id"]), int(gap["start_at"]), before=True
            )
            after_id = gap["after_message_id"]
            if after_id is None and gap["end_at"] is not None:
                after_id = self._message_boundary(
                    connection, str(gap["group_id"]), int(gap["end_at"]), before=False
                )
            connection.execute(
                """UPDATE message_gaps SET before_message_id = ?, after_message_id = ?,
                          updated_at = ? WHERE gap_id = ?""",
                (before_id, after_id, _utc_now(), gap_id),
            )
        return self.message_gap(gap_id)

    def message_gap(self, gap_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM message_gaps WHERE gap_id = ?", (gap_id,)
            ).fetchone()
        if row is None:
            raise KeyError("消息缺口不存在")
        return self._gap_from_row(row)

    def list_message_gaps(
        self,
        *,
        group_id: str | None = None,
        unresolved_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        clauses: list[str] = []
        parameters: list[object] = []
        if group_id is not None:
            clauses.append("group_id = ?")
            parameters.append(group_id)
        if unresolved_only:
            clauses.append("status NOT IN ('repaired', 'accepted')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM message_gaps {where}
                    ORDER BY start_at DESC, created_at DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return [self._gap_from_row(row) for row in rows]

    def message_gaps_overlapping(
        self, group_id: str, *, start_at: int, end_at: int
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM message_gaps
                   WHERE group_id = ?
                     AND status NOT IN ('repaired', 'accepted')
                     AND start_at <= ?
                     AND COALESCE(end_at, 9223372036854775807) >= ?
                   ORDER BY start_at""",
                (group_id, end_at, start_at),
            ).fetchall()
        return [self._gap_from_row(row) for row in rows]

    def accepted_message_gaps_overlapping(
        self, group_id: str, *, start_at: int, end_at: int, limit: int = 5
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        parameters = (group_id, end_at, start_at)
        with closing(self._connect()) as connection:
            count = connection.execute(
                """SELECT COUNT(*) FROM message_gaps
                   WHERE group_id = ?
                     AND status = 'accepted'
                     AND start_at <= ?
                     AND COALESCE(end_at, 9223372036854775807) >= ?""",
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT * FROM message_gaps
                   WHERE group_id = ?
                     AND status = 'accepted'
                     AND start_at <= ?
                     AND COALESCE(end_at, 9223372036854775807) >= ?
                   ORDER BY start_at DESC, created_at DESC
                   LIMIT ?""",
                (*parameters, limit),
            ).fetchall()
        gaps = [self._gap_from_row(row) for row in rows]
        return {
            "count": int(count),
            "gaps": gaps,
            "truncated": int(count) > len(gaps),
        }

    def unresolved_message_gaps_before(
        self, group_id: str, *, timestamp: int
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM message_gaps
                   WHERE group_id = ?
                     AND status NOT IN ('repaired', 'accepted')
                     AND COALESCE(end_at, start_at) < ?
                   ORDER BY start_at DESC""",
                (group_id, timestamp),
            ).fetchall()
        return [self._gap_from_row(row) for row in rows]

    def update_message_gap_repair(
        self,
        gap_id: str,
        *,
        status: str,
        repair_cursor: str | None = None,
        increment_pages: bool = False,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"open", "paused", "repairing", "repaired", "unverified"}:
            raise ValueError("无效的缺口修复状态")
        with closing(self._connect()) as connection, connection:
            result = connection.execute(
                """UPDATE message_gaps SET status = ?, repair_cursor = ?,
                          pages_attempted = pages_attempted + ?,
                          last_error = ?, updated_at = ?
                   WHERE gap_id = ?""",
                (
                    status,
                    repair_cursor,
                    int(increment_pages),
                    error[:500] if error else None,
                    _utc_now(),
                    gap_id,
                ),
            )
            if result.rowcount != 1:
                raise KeyError("消息缺口不存在")
        return self.message_gap(gap_id)

    def accept_message_gap(self, gap_id: str, *, reason: str) -> dict[str, Any]:
        text = reason.strip()
        if not text:
            raise ValueError("接受缺口必须填写原因")
        with closing(self._connect()) as connection, connection:
            result = connection.execute(
                """UPDATE message_gaps SET status = 'accepted',
                          accepted_reason = ?, updated_at = ?
                   WHERE gap_id = ? AND status != 'repaired'""",
                (text[:500], _utc_now(), gap_id),
            )
            if result.rowcount != 1:
                raise ValueError("缺口不存在或已经修复")
        return self.message_gap(gap_id)

    def pause_incomplete_gap_repairs(self) -> int:
        with closing(self._connect()) as connection, connection:
            result = connection.execute(
                """UPDATE message_gaps SET status = 'paused',
                          last_error = '应用重启后需要人工恢复', updated_at = ?
                   WHERE status = 'repairing'""",
                (_utc_now(),),
            )
        return int(result.rowcount)

    def record_onebot_action(self, record: dict[str, Any]) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO onebot_action_audit (
                       action, source, outcome, latency_ms, error_type, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(record.get("action") or "unknown")[:80],
                    str(record.get("source") or "unspecified")[:80],
                    str(record.get("outcome") or "error"),
                    max(0.0, float(record.get("latency_ms") or 0)),
                    str(record.get("error_type"))[:120] if record.get("error_type") else None,
                    str(record.get("created_at") or _utc_now()),
                ),
            )
            connection.execute("DELETE FROM onebot_action_audit WHERE created_at < ?", (cutoff,))

    def onebot_action_summary(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        result: dict[str, Any] = {}
        with closing(self._connect()) as connection:
            for label, delta in (
                ("last_24_hours", timedelta(hours=24)),
                ("last_7_days", timedelta(days=7)),
            ):
                rows = connection.execute(
                    """SELECT action, source, outcome, count(*) AS calls
                       FROM onebot_action_audit WHERE created_at >= ?
                       GROUP BY action, source, outcome
                       ORDER BY action, source, outcome""",
                    ((now - delta).isoformat(),),
                ).fetchall()
                result[label] = [dict(row) for row in rows]
        return result

    def history_actions_in_last_24_hours(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT count(*) FROM onebot_action_audit
                   WHERE action = 'get_group_msg_history' AND created_at >= ?""",
                (cutoff,),
            ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def register_qq_account(self, account_id: str, *, label: str) -> dict[str, Any]:
        if not account_id.isdigit():
            raise ValueError("QQ 号只能包含数字")
        text = label.strip()
        if not text or len(text) > 40:
            raise ValueError("账号标签必须是 1 到 40 字")
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO qq_accounts (
                       account_id, label, status, created_at, updated_at
                   ) VALUES (?, ?, 'registered', ?, ?)
                   ON CONFLICT (account_id) DO UPDATE SET
                       label = excluded.label,
                       status = CASE WHEN qq_accounts.status = 'disabled'
                                     THEN 'registered' ELSE qq_accounts.status END,
                       updated_at = excluded.updated_at""",
                (account_id, text, now, now),
            )
            groups = connection.execute(
                """SELECT DISTINCT group_key FROM group_member_roles
                   WHERE role = 'player'"""
            ).fetchall()
            connection.executemany(
                """INSERT OR IGNORE INTO group_member_roles (
                       group_key, qq_user_id, role, display_name
                   ) VALUES (?, ?, 'player', ?)""",
                [(str(row["group_key"]), account_id, text) for row in groups],
            )
        return self.qq_account(account_id)

    def qq_account(self, account_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM qq_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError("QQ 账号尚未登记")
        return self._account_from_row(row)

    def list_qq_accounts(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM qq_accounts
                   ORDER BY status = 'active' DESC, created_at, account_id"""
            ).fetchall()
        return [self._account_from_row(row) for row in rows]

    def ensure_active_qq_account(self, account_id: str) -> dict[str, Any]:
        try:
            self.qq_account(account_id)
        except KeyError:
            self.register_qq_account(account_id, label=f"QQ {account_id}")
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            active = connection.execute(
                "SELECT account_id FROM qq_accounts WHERE status = 'active'"
            ).fetchone()
            if active is None:
                connection.execute(
                    """UPDATE qq_accounts SET status = 'active',
                              last_activated_at = ?, updated_at = ?
                       WHERE account_id = ?""",
                    (now, now, account_id),
                )
            elif str(active["account_id"]) != account_id:
                connection.execute(
                    """UPDATE qq_accounts SET status = 'pending', updated_at = ?
                       WHERE account_id = ?""",
                    (now, account_id),
                )
        return self.qq_account(account_id)

    def active_qq_account(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM qq_accounts WHERE status = 'active'").fetchone()
        return self._account_from_row(row) if row else None

    def create_qq_account_switch(
        self, target_account_id: str, *, requested_by: str
    ) -> dict[str, Any]:
        target = self.qq_account(target_account_id)
        if target["status"] == "disabled":
            raise ValueError("目标 QQ 账号已停用")
        current = self.active_qq_account()
        if current and current["account_id"] == target_account_id:
            raise ValueError("目标 QQ 已经是当前活跃账号")
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        failed_cutoff = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        with closing(self._connect()) as connection, connection:
            stale_cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
            connection.execute(
                """UPDATE qq_account_switches SET status = 'cancelled',
                          completed_at = ?, error = '确认链接超时'
                   WHERE status = 'requested' AND requested_at < ?""",
                (_utc_now(), stale_cutoff),
            )
            in_progress = connection.execute(
                """SELECT 1 FROM qq_account_switches
                   WHERE status IN ('requested', 'host_pending', 'awaiting_login')"""
            ).fetchone()
            if in_progress:
                raise ValueError("已有账号切换正在进行")
            recent_failure = connection.execute(
                """SELECT requested_at FROM qq_account_switches
                   WHERE target_account_id = ? AND status = 'failed'
                     AND requested_at >= ? ORDER BY requested_at DESC LIMIT 1""",
                (target_account_id, failed_cutoff),
            ).fetchone()
            if recent_failure:
                raise ValueError("目标账号登录失败后正在 30 分钟冷却中")
            failures = connection.execute(
                """SELECT count(*) FROM qq_account_switches
                   WHERE target_account_id = ? AND status = 'failed'
                     AND requested_at >= ?""",
                (target_account_id, cutoff),
            ).fetchone()
            if int(failures[0]) >= 3:
                raise ValueError("目标账号 24 小时内失败次数已达 3 次")
            switch_id = f"sw_{secrets.token_urlsafe(12)}"
            connection.execute(
                """INSERT INTO qq_account_switches (
                       switch_id, from_account_id, target_account_id, status,
                       requested_by, requested_at
                   ) VALUES (?, ?, ?, 'requested', ?, ?)""",
                (
                    switch_id,
                    str(current["account_id"]) if current else None,
                    target_account_id,
                    requested_by[:200],
                    _utc_now(),
                ),
            )
        return self.qq_account_switch(switch_id)

    def qq_account_switch(self, switch_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM qq_account_switches WHERE switch_id = ?", (switch_id,)
            ).fetchone()
        if row is None:
            raise KeyError("账号切换记录不存在")
        return dict(row)

    def update_qq_account_switch(
        self, switch_id: str, *, status: str, error: str | None = None
    ) -> dict[str, Any]:
        if status not in {
            "host_pending",
            "awaiting_login",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("无效的账号切换状态")
        now = _utc_now()
        switch = self.qq_account_switch(switch_id)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE qq_account_switches SET status = ?,
                          confirmed_at = CASE WHEN ? = 'host_pending'
                                              THEN COALESCE(confirmed_at, ?)
                                              ELSE confirmed_at END,
                          completed_at = CASE WHEN ? IN ('completed', 'failed', 'cancelled')
                                              THEN ? ELSE completed_at END,
                          error = ?
                   WHERE switch_id = ?""",
                (
                    status,
                    status,
                    now,
                    status,
                    now,
                    error[:500] if error else None,
                    switch_id,
                ),
            )
            if status == "awaiting_login":
                connection.execute(
                    """UPDATE qq_accounts SET status = 'pending', updated_at = ?
                       WHERE account_id = ?""",
                    (now, switch["target_account_id"]),
                )
            if status == "completed":
                connection.execute(
                    """UPDATE qq_accounts SET status = 'registered', updated_at = ?
                       WHERE status = 'active'""",
                    (now,),
                )
                connection.execute(
                    """UPDATE qq_accounts SET status = 'active',
                              last_activated_at = ?, updated_at = ?
                       WHERE account_id = ?""",
                    (now, now, switch["target_account_id"]),
                )
        return self.qq_account_switch(switch_id)

    def latest_qq_account_switch(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM qq_account_switches
                   ORDER BY requested_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def upsert(self, messages: Iterable[ChatMessage]) -> tuple[int, int]:
        batch = list(messages)
        if not batch:
            return 0, 0
        if len({message.group_id for message in batch}) != 1:
            raise ValueError("一次 upsert 只能包含一个群的消息")
        now = _utc_now()
        group_id = batch[0].group_id
        ids = [message.message_id for message in batch]
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as connection:
            existing = {
                str(row[0])
                for row in connection.execute(
                    f"SELECT message_id FROM messages WHERE group_id = ? AND message_id IN ({placeholders})",
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
                       state.oldest_message_seq, state.reconcile_cursor,
                       state.reconcile_boundary_id, state.reconcile_newest_id,
                       state.recent_ready,
                       state.initial_import_complete, state.last_sync_at, state.last_error,
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
        result = dict(row)
        result["recent_ready"] = bool(result["recent_ready"])
        result["initial_import_complete"] = bool(result["initial_import_complete"])
        return result

    def update_state(
        self,
        *,
        account_id: str,
        group_id: str,
        latest_message_id: str | None = None,
        oldest_message_seq: str | None = None,
        reconcile_cursor: str | None = None,
        reconcile_boundary_id: str | None = None,
        reconcile_newest_id: str | None = None,
        clear_reconcile: bool = False,
        recent_ready: bool | None = None,
        initial_import_complete: bool | None = None,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO sync_state (
                    group_id, account_id, latest_message_id, oldest_message_seq,
                    reconcile_cursor, reconcile_boundary_id, reconcile_newest_id,
                    recent_ready, initial_import_complete, last_sync_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (group_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    latest_message_id = COALESCE(excluded.latest_message_id, sync_state.latest_message_id),
                    oldest_message_seq = COALESCE(excluded.oldest_message_seq, sync_state.oldest_message_seq),
                    reconcile_cursor = CASE
                        WHEN ? THEN NULL
                        ELSE COALESCE(excluded.reconcile_cursor, sync_state.reconcile_cursor) END,
                    reconcile_boundary_id = CASE
                        WHEN ? THEN NULL
                        ELSE COALESCE(
                            excluded.reconcile_boundary_id, sync_state.reconcile_boundary_id
                        ) END,
                    reconcile_newest_id = CASE
                        WHEN ? THEN NULL
                        ELSE COALESCE(excluded.reconcile_newest_id, sync_state.reconcile_newest_id)
                        END,
                    recent_ready = CASE WHEN ? IS NULL THEN sync_state.recent_ready ELSE excluded.recent_ready END,
                    initial_import_complete = CASE
                        WHEN ? IS NULL THEN sync_state.initial_import_complete
                        ELSE excluded.initial_import_complete END,
                    last_sync_at = excluded.last_sync_at,
                    last_error = excluded.last_error
                """,
                (
                    group_id,
                    account_id,
                    latest_message_id,
                    oldest_message_seq,
                    reconcile_cursor,
                    reconcile_boundary_id,
                    reconcile_newest_id,
                    int(bool(recent_ready)),
                    int(bool(initial_import_complete)),
                    now,
                    error,
                    int(clear_reconcile),
                    int(clear_reconcile),
                    int(clear_reconcile),
                    recent_ready,
                    initial_import_complete,
                ),
            )

    def record_error(self, *, account_id: str, group_id: str, error: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO sync_state (
                    group_id, account_id, recent_ready, initial_import_complete, last_error
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT (group_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    last_error = excluded.last_error
                """,
                (group_id, account_id, error[:500]),
            )

    def message_exists(self, group_id: str, message_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM messages WHERE group_id = ? AND message_id = ?",
                (group_id, message_id),
            ).fetchone()
        return row is not None

    def messages_exist(self, group_id: str, message_ids: Iterable[str]) -> bool:
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            return True
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as connection:
            count = connection.execute(
                f"SELECT count(*) FROM messages WHERE group_id = ? AND message_id IN ({placeholders})",
                (group_id, *ids),
            ).fetchone()[0]
        return int(count) == len(ids)

    def latest_message_id(self, group_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT message_id FROM messages WHERE group_id = ?
                   ORDER BY sent_at DESC, id DESC LIMIT 1""",
                (group_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def message_seq(self, group_id: str, message_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT message_seq FROM messages
                   WHERE group_id = ? AND message_id = ?""",
                (group_id, message_id),
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

    @staticmethod
    def _message_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        result = [dict(row) for row in rows]
        for item in result:
            item["contains_unsupported_media"] = bool(item["contains_unsupported_media"])
        return result

    def recent(
        self, group_id: str, *, limit: int, before_message_id: str | None = None
    ) -> list[dict[str, Any]]:
        before_clause = ""
        parameters: list[object] = [group_id]
        if before_message_id:
            before_clause = """
                AND (sent_at, id) < (
                    SELECT sent_at, id FROM messages WHERE group_id = ? AND message_id = ?
                )
            """
            parameters.extend([group_id, before_message_id])
        parameters.append(limit)
        with closing(self._connect()) as connection:
            if before_message_id:
                anchor = connection.execute(
                    "SELECT 1 FROM messages WHERE group_id = ? AND message_id = ?",
                    (group_id, before_message_id),
                ).fetchone()
                if anchor is None:
                    raise ValueError("before_message_id 不属于当前群或已不存在")
            rows = connection.execute(
                f"""SELECT message_id, sent_at, sender_id, sender_display, plain_text,
                            reply_to_message_id, contains_unsupported_media
                     FROM messages WHERE group_id = ? {before_clause}
                     ORDER BY sent_at DESC, id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return list(reversed(self._message_rows(rows)))

    def context_messages(
        self, group_id: str, *, since_message_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        if not since_message_id:
            return self.recent(group_id, limit=limit)
        with closing(self._connect()) as connection:
            anchor = connection.execute(
                "SELECT sent_at, id FROM messages WHERE group_id = ? AND message_id = ?",
                (group_id, since_message_id),
            ).fetchone()
            if anchor is None:
                raise ValueError("since_message_id 不属于当前群或已不存在")
            rows = connection.execute(
                """SELECT message_id, sent_at, sender_id, sender_display, plain_text,
                          reply_to_message_id, contains_unsupported_media
                   FROM messages WHERE group_id = ? AND (sent_at, id) > (?, ?)
                   ORDER BY sent_at ASC, id ASC LIMIT ?""",
                (group_id, int(anchor["sent_at"]), int(anchor["id"]), limit),
            ).fetchall()
        return self._message_rows(rows)

    def search(
        self,
        group_id: str,
        *,
        query: str | None,
        sender_id: str | None,
        start_timestamp: int | None,
        end_timestamp: int | None,
        limit: int,
        before_message_id: str | None = None,
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
        if before_message_id:
            if not self.message_exists(group_id, before_message_id):
                raise ValueError("before_message_id 不属于当前群或已不存在")
            clauses.append(
                """(sent_at, id) < (
                    SELECT sent_at, id FROM messages
                    WHERE group_id = ? AND message_id = ?
                )"""
            )
            parameters.extend([group_id, before_message_id])
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT message_id, sent_at, sender_id, sender_display, plain_text,
                            reply_to_message_id, contains_unsupported_media
                     FROM messages WHERE {" AND ".join(clauses)}
                     ORDER BY sent_at DESC, id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return list(reversed(self._message_rows(rows)))

    def character(self, group_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM characters WHERE group_key = ?", (group_key,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["base"] = _unjson(result.pop("base_json"), {})
        result["current"] = _unjson(result.pop("current_json"), {})
        return result

    def replace_character(
        self,
        group_key: str,
        *,
        source_filename: str,
        source_sha256: str,
        source_path: str,
        base_card: dict[str, Any],
        current_card: dict[str, Any],
        clear_runtime_data: bool,
    ) -> dict[str, Any]:
        _validate_card(base_card)
        _validate_card(current_card)
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT version FROM groups WHERE group_key = ? AND whitelisted = 1", (group_key,)
            ).fetchone()
            if row is None:
                raise KeyError("群尚未授权 AI 访问")
            connection.execute(
                """INSERT INTO characters (
                       group_key, source_filename, source_sha256, source_path,
                       base_json, current_json, imported_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (group_key) DO UPDATE SET
                       source_filename = excluded.source_filename,
                       source_sha256 = excluded.source_sha256,
                       source_path = excluded.source_path,
                       base_json = excluded.base_json,
                       current_json = excluded.current_json,
                       imported_at = excluded.imported_at""",
                (
                    group_key,
                    source_filename,
                    source_sha256,
                    source_path,
                    _json(base_card),
                    _json(current_card),
                    now,
                ),
            )
            if clear_runtime_data:
                connection.execute("DELETE FROM campaign_notes WHERE group_key = ?", (group_key,))
                connection.execute("DELETE FROM group_changes WHERE group_key = ?", (group_key,))
            connection.execute(
                """UPDATE groups SET version = version + 1, updated_at = ?
                   WHERE group_key = ?""",
                (now, group_key),
            )
        result = self.character(group_key)
        assert result is not None
        return result

    def notes(self, group_key: str, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        where = "" if include_resolved else "AND status = 'active'"
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM campaign_notes WHERE group_key = ? {where}
                   ORDER BY category, updated_at DESC, note_id""",
                (group_key,),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["source_message_ids"] = _unjson(item.pop("source_message_ids_json"), [])
        return result

    @staticmethod
    def _note_row(connection: sqlite3.Connection, note_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM campaign_notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["source_message_ids"] = _unjson(result.pop("source_message_ids_json"), [])
        return result

    def commit_turn_updates(
        self,
        group_key: str,
        *,
        expected_version: int,
        origin: str,
        card_operations: list[CardOperation],
        note_operations: list[NoteOperation],
        summary: str,
    ) -> dict[str, Any]:
        if origin not in {"qq_event", "user_request"}:
            raise ValueError("origin 必须是 qq_event 或 user_request")
        if not card_operations and not note_operations:
            raise ValueError("至少提供一项人物卡或笔记变更")
        summary = summary.strip()
        if not summary or len(summary) > 500:
            raise ValueError("summary 必须是 1..500 字")

        group = self.get_group(group_key)
        group_id = str(group["qq_group_id"])
        all_sources = [
            item for card_op in card_operations for item in card_op.source_message_ids
        ] + [item for note_op in note_operations for item in note_op.source_message_ids]
        if origin == "qq_event" and any(not op.source_message_ids for op in card_operations):
            raise ValueError("自动人物卡变更必须提供 source_message_ids")
        if not self.messages_exist(group_id, all_sources):
            raise ValueError("source_message_ids 包含不属于当前群的消息")

        now = _utc_now()
        change_id = f"chg_{secrets.token_urlsafe(12)}"
        touched_card: dict[str, dict[str, Any]] = {}
        touched_notes: dict[str, dict[str, Any] | None] = {}
        rendered_note_ops: list[dict[str, Any]] = []

        with closing(self._connect()) as connection, connection:
            current_version = self._expect_version(connection, group_key, expected_version)
            character_row = connection.execute(
                "SELECT current_json FROM characters WHERE group_key = ?", (group_key,)
            ).fetchone()
            card: dict[str, Any] | None = (
                _unjson(character_row["current_json"], {}) if character_row else None
            )
            if card_operations and card is None:
                raise ValueError("当前群尚未上传人物卡")

            assert card is not None or not card_operations
            if card is not None:
                for card_op in card_operations:
                    _validate_card_path(card_op.path, origin)
                    restore_path = card_op.path
                    if card_op.op == "add":
                        restore_path = card_op.path
                    if restore_path not in touched_card:
                        exists, old = _pointer_get(card, restore_path)
                        touched_card[restore_path] = {"exists": exists, "value": old}
                    if card_op.op == "set":
                        _pointer_set(card, card_op.path, card_op.value)
                    elif card_op.op == "increment":
                        exists, old = _pointer_get(card, card_op.path)
                        if not exists or isinstance(old, bool) or not isinstance(old, (int, float)):
                            raise ValueError(f"increment 目标不是数字：{card_op.path}")
                        if isinstance(card_op.value, bool) or not isinstance(
                            card_op.value, (int, float)
                        ):
                            raise ValueError("increment 的 value 必须是数字")
                        _pointer_set(card, card_op.path, old + card_op.value)
                    elif card_op.op == "add":
                        exists, target = _pointer_get(card, card_op.path)
                        if not exists or not isinstance(target, list):
                            raise ValueError(f"add 目标不是列表：{card_op.path}")
                        target.append(copy.deepcopy(card_op.value))
                        _pointer_set(card, card_op.path, target)
                    else:
                        exists, _ = _pointer_get(card, card_op.path)
                        if not exists:
                            raise ValueError(f"remove 目标不存在：{card_op.path}")
                        _pointer_remove(card, card_op.path)
                _validate_card(card)

            for note_op in note_operations:
                note_id = note_op.note_id
                if note_op.op == "create":
                    note_id = f"note_{secrets.token_urlsafe(10)}"
                assert note_id is not None
                old_note = self._note_row(connection, note_id)
                if note_id not in touched_notes:
                    touched_notes[note_id] = old_note
                if note_op.op == "create":
                    connection.execute(
                        """INSERT INTO campaign_notes (
                               note_id, group_key, category, title, content, status,
                               source_message_ids_json, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                        (
                            note_id,
                            group_key,
                            note_op.category,
                            note_op.title,
                            note_op.content,
                            _json(note_op.source_message_ids),
                            now,
                            now,
                        ),
                    )
                else:
                    if old_note is None or old_note["group_key"] != group_key:
                        raise ValueError(f"笔记不存在或不属于当前群：{note_id}")
                    if note_op.op == "delete":
                        connection.execute(
                            "DELETE FROM campaign_notes WHERE note_id = ?", (note_id,)
                        )
                    elif note_op.op == "resolve":
                        connection.execute(
                            "UPDATE campaign_notes SET status = 'resolved', updated_at = ? WHERE note_id = ?",
                            (now, note_id),
                        )
                    else:
                        updates: dict[str, Any] = {}
                        for field in ("category", "title", "content"):
                            value = getattr(note_op, field)
                            if value is not None:
                                updates[field] = value
                        if note_op.source_message_ids:
                            updates["source_message_ids_json"] = _json(note_op.source_message_ids)
                        assignments = ", ".join(f"{key} = ?" for key in updates)
                        connection.execute(
                            f"UPDATE campaign_notes SET {assignments}, updated_at = ? WHERE note_id = ?",
                            (*updates.values(), now, note_id),
                        )
                rendered = note_op.model_dump(mode="json")
                rendered["note_id"] = note_id
                rendered_note_ops.append(rendered)

            if card is not None and card_operations:
                connection.execute(
                    "UPDATE characters SET current_json = ? WHERE group_key = ?",
                    (_json(card), group_key),
                )
            new_version = current_version + 1
            connection.execute(
                "UPDATE groups SET version = ?, updated_at = ? WHERE group_key = ?",
                (new_version, now, group_key),
            )
            operations = {
                "card_operations": [item.model_dump(mode="json") for item in card_operations],
                "note_operations": rendered_note_ops,
            }
            touched = {"card_paths": sorted(touched_card), "note_ids": sorted(touched_notes)}
            before = {"card": touched_card, "notes": touched_notes}
            connection.execute(
                """INSERT INTO group_changes (
                       change_id, group_key, origin, summary, base_version, new_version,
                       operations_json, before_json, touched_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    change_id,
                    group_key,
                    origin,
                    summary,
                    current_version,
                    new_version,
                    _json(operations),
                    _json(before),
                    _json(touched),
                    now,
                ),
            )

        return {
            "change_id": change_id,
            "base_version": expected_version,
            "new_version": expected_version + 1,
            "summary": summary,
            "card_operation_count": len(card_operations),
            "note_operation_count": len(note_operations),
            "undoable": True,
        }

    def list_changes(
        self, group_key: str, *, limit: int, before_change_id: str | None = None
    ) -> list[dict[str, Any]]:
        before = ""
        parameters: list[Any] = [group_key]
        if before_change_id:
            before = "AND id < (SELECT id FROM group_changes WHERE change_id = ?)"
            parameters.append(before_change_id)
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT id, change_id, origin, summary, base_version, new_version,
                           operations_json, undone_by, created_at
                    FROM group_changes WHERE group_key = ? {before}
                    ORDER BY id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item.pop("id", None)
            item["operations"] = _unjson(item.pop("operations_json"), {})
            item["undone"] = bool(item["undone_by"])
        return result

    def undo_change(
        self,
        group_key: str,
        *,
        change_id: str,
        expected_version: int,
        reason: str,
    ) -> dict[str, Any]:
        reason = reason.strip()
        if not reason or len(reason) > 500:
            raise ValueError("reason 必须是 1..500 字")
        undo_id = f"chg_{secrets.token_urlsafe(12)}"
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            current_version = self._expect_version(connection, group_key, expected_version)
            row = connection.execute(
                "SELECT * FROM group_changes WHERE group_key = ? AND change_id = ?",
                (group_key, change_id),
            ).fetchone()
            if row is None:
                raise ValueError("找不到指定 change_id")
            if row["undone_by"]:
                raise ValueError("该变更已经撤销")
            touched = _unjson(row["touched_json"], {"card_paths": [], "note_ids": []})
            later = connection.execute(
                """SELECT change_id, touched_json FROM group_changes
                   WHERE group_key = ? AND id > ? AND undone_by IS NULL""",
                (group_key, int(row["id"])),
            ).fetchall()
            wanted_card = set(touched["card_paths"])
            wanted_notes = set(touched["note_ids"])
            conflicts: list[str] = []
            for item in later:
                later_touched = _unjson(item["touched_json"], {"card_paths": [], "note_ids": []})
                if wanted_card.intersection(
                    later_touched["card_paths"]
                ) or wanted_notes.intersection(later_touched["note_ids"]):
                    conflicts.append(str(item["change_id"]))
            if conflicts:
                raise ValueError(
                    "该变更的字段后来又被修改，不能安全自动撤销；冲突变更："
                    + ", ".join(conflicts[:5])
                )

            before = _unjson(row["before_json"], {"card": {}, "notes": {}})
            character_row = connection.execute(
                "SELECT current_json FROM characters WHERE group_key = ?", (group_key,)
            ).fetchone()
            card = _unjson(character_row["current_json"], {}) if character_row else None
            undo_before_card: dict[str, Any] = {}
            if before["card"]:
                if card is None:
                    raise ValueError("当前人物卡不存在，无法撤销")
                for path, old in before["card"].items():
                    exists, current = _pointer_get(card, path)
                    undo_before_card[path] = {"exists": exists, "value": current}
                    if old["exists"]:
                        _pointer_set(card, path, old["value"])
                    elif exists:
                        _pointer_remove(card, path)
                _validate_card(card)
                connection.execute(
                    "UPDATE characters SET current_json = ? WHERE group_key = ?",
                    (_json(card), group_key),
                )

            undo_before_notes: dict[str, Any] = {}
            for note_id, old_note in before["notes"].items():
                undo_before_notes[note_id] = self._note_row(connection, note_id)
                connection.execute("DELETE FROM campaign_notes WHERE note_id = ?", (note_id,))
                if old_note is not None:
                    connection.execute(
                        """INSERT INTO campaign_notes (
                               note_id, group_key, category, title, content, status,
                               source_message_ids_json, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            old_note["note_id"],
                            old_note["group_key"],
                            old_note["category"],
                            old_note["title"],
                            old_note["content"],
                            old_note["status"],
                            _json(old_note["source_message_ids"]),
                            old_note["created_at"],
                            now,
                        ),
                    )

            new_version = current_version + 1
            connection.execute(
                "UPDATE groups SET version = ?, updated_at = ? WHERE group_key = ?",
                (new_version, now, group_key),
            )
            connection.execute(
                "UPDATE group_changes SET undone_by = ? WHERE change_id = ?", (undo_id, change_id)
            )
            connection.execute(
                """INSERT INTO group_changes (
                       change_id, group_key, origin, summary, base_version, new_version,
                       operations_json, before_json, touched_json, created_at
                   ) VALUES (?, ?, 'undo', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    undo_id,
                    group_key,
                    f"撤销 {change_id}：{reason}",
                    current_version,
                    new_version,
                    _json({"undoes": change_id}),
                    _json({"card": undo_before_card, "notes": undo_before_notes}),
                    row["touched_json"],
                    now,
                ),
            )
        return {
            "change_id": undo_id,
            "undid_change_id": change_id,
            "new_version": expected_version + 1,
            "summary": f"已撤销 {change_id}",
        }

    def issue_capability(
        self,
        *,
        kind: str,
        group_key: str | None,
        issued_to: str,
        ttl_seconds: int,
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO capability_tokens (
                       token_hash, kind, group_key, issued_to, expires_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    _token_hash(token),
                    kind,
                    group_key,
                    issued_to,
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                    now.isoformat(),
                ),
            )
        return token

    def capability(self, token: str, *, kind: str, consume: bool = False) -> dict[str, Any]:
        now = datetime.now(UTC)
        token_digest = _token_hash(token)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM capability_tokens WHERE token_hash = ?", (token_digest,)
            ).fetchone()
            if row is None or row["kind"] != kind:
                raise ValueError("链接无效")
            if row["consumed_at"]:
                raise ValueError("链接已经使用")
            if datetime.fromisoformat(str(row["expires_at"])) <= now:
                raise ValueError("链接已经过期")
            if row["group_key"]:
                self.get_group(str(row["group_key"]))
            if consume:
                connection.execute(
                    "UPDATE capability_tokens SET consumed_at = ? WHERE token_hash = ?",
                    (now.isoformat(), token_digest),
                )
        result = dict(row)
        result["payload"] = _unjson(result.pop("payload_json"), {})
        return result

    def set_capability_payload(self, token: str, *, kind: str, payload: dict[str, Any]) -> None:
        self.capability(token, kind=kind)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE capability_tokens SET payload_json = ? WHERE token_hash = ?",
                (_json(payload), _token_hash(token)),
            )
