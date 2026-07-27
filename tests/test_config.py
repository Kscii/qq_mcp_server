from __future__ import annotations

from pathlib import Path

import pytest

from qq_mcp_server.config import ConfigError, default_config_text, load_config


def test_load_multi_group_config_without_target_group(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(default_config_text(account_id="123"), encoding="utf-8")
    config = load_config(path)
    assert config.account_id == "123"
    assert config.database_path == tmp_path / "data/trpg.sqlite3"
    assert config.card_storage_dir == tmp_path / "data/cards"
    assert config.rules_database_path == tmp_path / "data/rules.sqlite3"
    assert "group_id" not in default_config_text(account_id="123")
    assert "export" not in default_config_text(account_id="123")


def test_public_url_requires_https_and_email(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    text = default_config_text(account_id="123").replace(
        '# public_url = "https://qq-mcp.example.com"', 'public_url = "http://bad.example"'
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="HTTPS"):
        load_config(path)


def test_onebot_must_remain_loopback(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    text = default_config_text(account_id="123").replace(
        "http://127.0.0.1:3000", "http://qq.example:3000"
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="回环"):
        load_config(path)


def test_onebot_can_use_private_tailscale_https(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    text = (
        default_config_text(account_id="123")
        .replace(
            "http://127.0.0.1:3000",
            "https://collector.example-tailnet.ts.net:8444",
        )
        .replace(
            "http://127.0.0.1:3001/_events",
            "https://collector.example-tailnet.ts.net:8445/_events",
        )
    )
    path.write_text(text, encoding="utf-8")
    config = load_config(path)
    assert config.onebot_url.endswith(":8444")


def test_napcat_webui_must_be_private_tailscale_https(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    text = default_config_text(account_id="123").replace(
        '# napcat_webui_url = "https://qq-mcp-server.example-tailnet.ts.net:8443/webui"',
        'napcat_webui_url = "https://public.example.com/webui"',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="ts.net"):
        load_config(path)
