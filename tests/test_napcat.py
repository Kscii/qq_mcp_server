from __future__ import annotations

import json
from pathlib import Path

from qq_mcp_server.napcat import prepare_napcat_config


def test_napcat_config_is_loopback_http_only_and_has_no_send_action(tmp_path: Path) -> None:
    prepare_napcat_config(tmp_path, "secret-token")
    onebot = json.loads((tmp_path / "onebot11.json").read_text(encoding="utf-8"))
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
    assert "send" not in json.dumps(onebot).lower()


def test_prepare_keeps_webui_login_token_but_rotates_onebot_token(tmp_path: Path) -> None:
    prepare_napcat_config(tmp_path, "first")
    first_webui = (tmp_path / "webui.json").read_text(encoding="utf-8")
    prepare_napcat_config(tmp_path, "second")
    assert (tmp_path / "webui.json").read_text(encoding="utf-8") == first_webui
    onebot = json.loads((tmp_path / "onebot11.json").read_text(encoding="utf-8"))
    assert onebot["network"]["httpServers"][0]["token"] == "second"
