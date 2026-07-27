from __future__ import annotations

from pathlib import Path

import pytest

from qq_mcp_server.config import AppConfig


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        account_id="1",
        onebot_url="http://127.0.0.1:3000",
        onebot_sse_url="http://127.0.0.1:3001/_events",
        poll_interval_seconds=60,
        registry_refresh_seconds=1,
        group_discovery_interval_seconds=900,
        context_freshness_seconds=180,
        sync_concurrency=1,
        page_size=3,
        backfill_min_delay_seconds=0,
        backfill_max_delay_seconds=0,
        backfill_pages_per_cycle=3,
        unreachable_backoff_max_seconds=900,
        initial_collection_paused=False,
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
        napcat_webui_url=None,
        napcat_webui_config_path=tmp_path / "napcat" / "config" / "webui.json",
        napcat_control_dir=tmp_path / "control",
        allowed_google_emails=(),
        oauth_storage_dir=tmp_path / "oauth",
    )
