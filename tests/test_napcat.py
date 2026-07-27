from __future__ import annotations

import json
from pathlib import Path

from qq_mcp_server.napcat import prepare_napcat_config


def test_napcat_config_is_loopback_http_sse_only_and_has_no_send_action(
    tmp_path: Path,
) -> None:
    prepare_napcat_config(tmp_path, "secret-token", "123456789")
    onebot = json.loads((tmp_path / "onebot11.json").read_text(encoding="utf-8"))
    account_onebot = json.loads((tmp_path / "onebot11_123456789.json").read_text(encoding="utf-8"))
    assert account_onebot == onebot
    network = onebot["network"]
    assert network["websocketServers"] == []
    assert network["httpClients"] == []
    assert network["httpServers"] == [
        {
            "enable": True,
            "name": "qq_mcp_server_read_only",
            "host": "127.0.0.1",
            "port": 3000,
            "enableCors": False,
            "enableWebsocket": False,
            "messagePostFormat": "array",
            "token": "secret-token",
            "debug": False,
        }
    ]
    assert network["httpSseServers"] == [
        {
            "enable": True,
            "name": "qq_mcp_server_events",
            "host": "127.0.0.1",
            "port": 3001,
            "enableCors": False,
            "enableWebsocket": False,
            "messagePostFormat": "array",
            "token": "secret-token",
            "debug": False,
            "reportSelfMessage": True,
        }
    ]
    assert "send" not in json.dumps(onebot).lower()
    for filename in ("napcat.json", "napcat_123456789.json"):
        log_config = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
        assert log_config["fileLog"] is False
        assert log_config["consoleLog"] is True
        assert log_config["fileLogLevel"] == "error"
        assert log_config["consoleLogLevel"] == "error"


def test_prepare_keeps_webui_login_token_but_rotates_onebot_token(tmp_path: Path) -> None:
    account_config = tmp_path / "napcat_123456789.json"
    account_config.write_text('{"autoTimeSync": true, "consoleLogLevel": "info"}')
    prepare_napcat_config(tmp_path, "first", "123456789")
    first_webui = (tmp_path / "webui.json").read_text(encoding="utf-8")
    prepare_napcat_config(tmp_path, "second", "123456789")
    assert (tmp_path / "webui.json").read_text(encoding="utf-8") == first_webui
    onebot = json.loads((tmp_path / "onebot11.json").read_text(encoding="utf-8"))
    assert onebot["network"]["httpServers"][0]["token"] == "second"
    account_onebot = json.loads((tmp_path / "onebot11_123456789.json").read_text(encoding="utf-8"))
    assert account_onebot == onebot
    updated = json.loads(account_config.read_text(encoding="utf-8"))
    assert updated["autoTimeSync"] is True
    assert updated["consoleLogLevel"] == "error"
