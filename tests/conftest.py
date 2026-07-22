from __future__ import annotations

from pathlib import Path

import pytest

from qq_mcp_server.config import AppConfig


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        account_id="1",
        onebot_url="http://127.0.0.1:3000",
        poll_interval_seconds=15,
        registry_refresh_seconds=1,
        sync_concurrency=2,
        page_size=3,
        request_timeout_seconds=20,
        history_timeout_seconds=90,
        history_since=None,
        database_path=tmp_path / "trpg.sqlite3",
        card_storage_dir=tmp_path / "cards",
        rules_database_path=tmp_path / "rules.sqlite3",
        timezone="Asia/Shanghai",
        upload_token_ttl_seconds=600,
        host="127.0.0.1",
        port=8000,
        public_url=None,
        allowed_google_emails=(),
        oauth_storage_dir=tmp_path / "oauth",
    )
