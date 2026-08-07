#!/usr/bin/env python3
"""Release automation that keeps app-only changes away from the collector."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from typing import Any

COLLECTOR_RUNTIME_PATHS = {
    "Dockerfile",
    "src/qq_mcp_server/application.py",
    "src/qq_mcp_server/cli.py",
    "src/qq_mcp_server/collector.py",
    "src/qq_mcp_server/config.py",
    "src/qq_mcp_server/gaps.py",
    "src/qq_mcp_server/models.py",
    "src/qq_mcp_server/normalization.py",
    "src/qq_mcp_server/onebot.py",
    "src/qq_mcp_server/runtime.py",
    "src/qq_mcp_server/store.py",
    "src/qq_mcp_server/sync.py",
    "deploy/compose.yaml",
    "deploy/config.server.toml",
}


def without_release_version(document: bytes, path: str) -> dict[str, Any]:
    parsed = tomllib.loads(document.decode())
    if path == "pyproject.toml":
        project = parsed.get("project")
        if isinstance(project, dict):
            project.pop("version", None)
    elif path == "uv.lock":
        packages = parsed.get("package")
        if isinstance(packages, list):
            for package in packages:
                if not isinstance(package, dict):
                    continue
                source = package.get("source")
                if package.get("name") == "qq-mcp-server" and source == {"editable": "."}:
                    package.pop("version", None)
    return parsed


def _git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def collector_image_needed(previous: str, current: str) -> bool:
    changed = set(_git("diff", "--name-only", previous, current, "--").decode().splitlines())
    if changed & COLLECTOR_RUNTIME_PATHS:
        return True
    for path in ("pyproject.toml", "uv.lock"):
        if path not in changed:
            continue
        old = without_release_version(_git("show", f"{previous}:{path}"), path)
        new = without_release_version(_git("show", f"{current}:{path}"), path)
        if old != new:
            return True
    return False


def main() -> int:
    if len(sys.argv) != 3:
        print("用法：python -m qq_mcp_server.release <previous-ref> <current-ref>", file=sys.stderr)
        return 2
    try:
        needed = collector_image_needed(sys.argv[1], sys.argv[2])
    except (OSError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        print(f"无法可靠判断采集器变更，采用安全更新：{exc}", file=sys.stderr)
        needed = True
    print("1" if needed else "0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
