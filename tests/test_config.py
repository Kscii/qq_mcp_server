from __future__ import annotations

from pathlib import Path

import pytest

from qq_mcp_server.config import ConfigError, default_config_text, load_config


def write_config(path: Path, *, onebot_url: str = "http://127.0.0.1:3000") -> None:
    text = default_config_text(account_id="123", group_id="456", group_name="群")
    path.write_text(text.replace("http://127.0.0.1:3000", onebot_url), encoding="utf-8")


def test_loads_minimal_config_and_resolves_paths(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    config = load_config(path)
    assert config.account_id == "123"
    assert config.group_id == "456"
    assert config.database_path == tmp_path / "data/messages.sqlite3"
    assert config.export_path == tmp_path / "data/groups/456.txt"


def test_environment_can_select_target_without_editing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    monkeypatch.setenv("QQ_GROUP_ID", "789")
    assert load_config(path).group_id == "789"


def test_rejects_non_loopback_onebot(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, onebot_url="http://public.invalid:3000")
    with pytest.raises(ConfigError, match="回环"):
        load_config(path)


def test_public_url_requires_https_and_email(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8").replace(
        '# public_url = "https://qq-mcp.example.com"',
        'public_url = "http://example.com"',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="HTTPS"):
        load_config(path)
