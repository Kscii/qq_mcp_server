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


def prepare_napcat_config(directory: Path, onebot_token: str, account_id: str) -> None:
    """生成仅监听回环地址的只读 HTTP 与 HTTP-SSE NapCat 配置。"""
    if not onebot_token:
        raise ValueError("ONEBOT_ACCESS_TOKEN 不能为空")
    if not account_id.isdigit():
        raise ValueError("QQ 账号必须只包含数字")
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
    _enforce_quiet_logs(directory / "napcat.json")
    _enforce_quiet_logs(directory / f"napcat_{account_id}.json")
    onebot = _onebot_config(onebot_token)
    _atomic_json(directory / "onebot11.json", onebot)
    # NapCat 登录后优先读取账号专属文件；只更新通用文件不会改变已登录账号的网络配置。
    _atomic_json(directory / f"onebot11_{account_id}.json", onebot)


def _enforce_quiet_logs(path: Path) -> None:
    value: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            value = loaded
    value.update(
        {
            "fileLog": False,
            "consoleLog": True,
            "fileLogLevel": "error",
            "consoleLogLevel": "error",
        }
    )
    _atomic_json(path, value)


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
            "httpSseServers": [
                {
                    "enable": True,
                    "name": "qq_mcp_server_events",
                    "host": "127.0.0.1",
                    "port": 3001,
                    "enableCors": False,
                    "enableWebsocket": False,
                    "messagePostFormat": "array",
                    "token": token,
                    "debug": False,
                    "reportSelfMessage": True,
                }
            ],
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
