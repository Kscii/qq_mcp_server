from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """配置缺失或不安全。"""


@dataclass(frozen=True, slots=True)
class AppConfig:
    account_id: str
    onebot_url: str
    poll_interval_seconds: float
    registry_refresh_seconds: float
    sync_concurrency: int
    page_size: int
    request_timeout_seconds: float
    history_timeout_seconds: float
    history_since: str | None
    database_path: Path
    card_storage_dir: Path
    rules_database_path: Path
    timezone: str
    upload_token_ttl_seconds: int
    host: str
    port: int
    public_url: str | None
    allowed_google_emails: tuple[str, ...]
    oauth_storage_dir: Path


def _table(raw: object, name: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] 必须是 TOML 表")
    return raw


def _text(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} 必须是非空字符串")
    return value.strip()


def _env(name: str, fallback: object) -> object:
    value = os.environ.get(name)
    return fallback if value is None else value


def _path(value: object, name: str, base: Path) -> Path:
    text = _text(value, name)
    assert text is not None
    path = Path(text).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _require_digits(value: object, name: str) -> str:
    text = _text(value, name)
    assert text is not None
    if not text.isdigit():
        raise ConfigError(f"{name} 只能包含数字")
    return text


def _number(value: object, name: str, minimum: float, maximum: float) -> float:
    try:
        result = float(str(value))
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{name} 必须是数字") from error
    if not minimum <= result <= maximum:
        raise ConfigError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return result


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{name} 必须是整数") from error
    if not minimum <= result <= maximum:
        raise ConfigError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return result


def load_config(path: Path) -> AppConfig:
    path = path.expanduser().resolve()
    try:
        with path.open("rb") as file:
            root = tomllib.load(file)
    except FileNotFoundError as error:
        raise ConfigError(f"配置文件不存在：{path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"配置文件不是有效 TOML：{error}") from error

    qq = _table(root.get("qq"), "qq")
    storage = _table(root.get("storage"), "storage")
    server = _table(root.get("server", {}), "server")
    access = _table(root.get("access", {}), "access")
    base = path.parent

    account_id = _require_digits(_env("QQ_ACCOUNT_ID", qq.get("account_id")), "qq.account_id")
    onebot_url = str(_env("ONEBOT_URL", qq.get("onebot_url", "http://127.0.0.1:3000")))
    parsed = urlsplit(onebot_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigError("qq.onebot_url 必须是本机回环 HTTP 地址")

    raw_emails = access.get("allowed_google_emails", [])
    if not isinstance(raw_emails, list) or any(not isinstance(item, str) for item in raw_emails):
        raise ConfigError("access.allowed_google_emails 必须是字符串列表")
    env_email = os.environ.get("ALLOWED_GOOGLE_EMAIL")
    emails = [env_email] if env_email else raw_emails
    normalized_emails = tuple(sorted({item.strip().lower() for item in emails if item.strip()}))

    raw_public_url = _env("PUBLIC_URL", server.get("public_url"))
    public_url = _text(raw_public_url, "server.public_url", optional=True)
    if public_url:
        public_url = public_url.rstrip("/")
        if urlsplit(public_url).scheme != "https":
            raise ConfigError("配置公网 MCP 时 server.public_url 必须使用 HTTPS")
        if not normalized_emails:
            raise ConfigError("配置公网 MCP 时必须设置 access.allowed_google_emails")

    return AppConfig(
        account_id=account_id,
        onebot_url=onebot_url.rstrip("/"),
        poll_interval_seconds=_number(
            qq.get("poll_interval_seconds", 15), "qq.poll_interval_seconds", 5, 300
        ),
        registry_refresh_seconds=_number(
            qq.get("registry_refresh_seconds", 5), "qq.registry_refresh_seconds", 1, 60
        ),
        sync_concurrency=_integer(qq.get("sync_concurrency", 3), "qq.sync_concurrency", 1, 16),
        page_size=_integer(qq.get("page_size", 100), "qq.page_size", 1, 500),
        request_timeout_seconds=_number(
            qq.get("request_timeout_seconds", 20), "qq.request_timeout_seconds", 1, 120
        ),
        history_timeout_seconds=_number(
            qq.get("history_timeout_seconds", 90), "qq.history_timeout_seconds", 5, 600
        ),
        history_since=_text(qq.get("history_since"), "qq.history_since", optional=True),
        database_path=_path(
            _env("DATABASE_PATH", storage.get("database", "data/trpg.sqlite3")),
            "storage.database",
            base,
        ),
        card_storage_dir=_path(
            _env("CARD_STORAGE_DIR", storage.get("cards", "data/cards")),
            "storage.cards",
            base,
        ),
        rules_database_path=_path(
            _env("RULES_DATABASE_PATH", storage.get("rules", "data/rules.sqlite3")),
            "storage.rules",
            base,
        ),
        timezone=str(storage.get("timezone") or "Asia/Shanghai"),
        upload_token_ttl_seconds=_integer(
            server.get("upload_token_ttl_seconds", 600),
            "server.upload_token_ttl_seconds",
            60,
            3600,
        ),
        host=str(server.get("host") or "127.0.0.1"),
        port=_integer(_env("PORT", server.get("port", 8000)), "server.port", 1, 65535),
        public_url=public_url,
        allowed_google_emails=normalized_emails,
        oauth_storage_dir=_path(
            _env("OAUTH_STORAGE_DIR", storage.get("oauth", "data/oauth")),
            "storage.oauth",
            base,
        ),
    )


def default_config_text(*, account_id: str) -> str:
    return f'''# 群白名单在管理 MCP 发起的极简网页中维护，不在这里填写群号。
[qq]
account_id = "{account_id}"
onebot_url = "http://127.0.0.1:3000"
poll_interval_seconds = 15
registry_refresh_seconds = 5
sync_concurrency = 3
page_size = 100
request_timeout_seconds = 20
history_timeout_seconds = 90
# 留空表示后台回填 NapCat 可获取的全部历史。
# history_since = "2026-01-01T00:00:00+08:00"

[storage]
database = "data/trpg.sqlite3"
cards = "data/cards"
rules = "data/rules.sqlite3"
timezone = "Asia/Shanghai"
oauth = "data/oauth"

[server]
host = "127.0.0.1"
port = 8000
upload_token_ttl_seconds = 600
# public_url = "https://qq-mcp.example.com"

[access]
# allowed_google_emails = ["you@example.com"]
'''
