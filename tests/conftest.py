from __future__ import annotations

from pathlib import Path

import pytest

from qq_mcp_server.config import AppConfig


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        account_id="1",
        group_id="2",
        group_name="测试群",
        onebot_url="http://127.0.0.1:3000",
        poll_interval_seconds=15,
        page_size=3,
        request_timeout_seconds=20,
        history_timeout_seconds=90,
        history_since=None,
        database_path=tmp_path / "messages.sqlite3",
        export_path=tmp_path / "2.txt",
        timezone="Asia/Shanghai",
        host="127.0.0.1",
        port=8000,
        public_url=None,
        allowed_google_emails=(),
        oauth_storage_dir=tmp_path / "oauth",
    )
