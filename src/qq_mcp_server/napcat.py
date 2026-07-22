from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def prepare_napcat_config(directory: Path, onebot_token: str) -> None:
    """生成仅监听回环地址、仅启用 HTTP 的 NapCat 配置。"""
    if not onebot_token:
        raise ValueError("ONEBOT_ACCESS_TOKEN 不能为空")
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    webui_path = directory / "webui.json"
    if not webui_path.exists():
        _atomic_json(
            webui_path,
            {
                "host": "127.0.0.1",
                "prefix": "",
                "port": 6099,
                "token": secrets.token_urlsafe(32),
                "loginRate": 3,
            },
        )
    _atomic_json(directory / "onebot11.json", _onebot_config(onebot_token))


def _onebot_config(token: str) -> dict[str, Any]:
    return {
        "network": {
            "httpServers": [
                {
                    "enable": True,
                    "name": "qq_mcp_server_read_only",
                    "host": "127.0.0.1",
                    "port": 3000,
                    "enableCors": False,
                    "enableWebsocket": False,
                    "messagePostFormat": "array",
                    "token": token,
                    "debug": False,
                }
            ],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
    }
