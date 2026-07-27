#!/bin/sh
set -eu

request=/var/lib/qq_mcp_server/control/switch-napcat-account.request
status=/var/lib/qq_mcp_server/control/switch-napcat-account.status.json
state_dir=/var/lib/qq-mcp-account-switch
lock="$state_dir/switch.lock"
deploy_dir=/opt/qq_mcp_server
env_file="$deploy_dir/.env"
expected_uid=1001
expected_gid=1002
expected_mode=600

write_status() {
    result="$1"
    message="$2"
    switch_id="${3:-}"
    temporary="$state_dir/status.$$"
    python3 - "$result" "$message" "$switch_id" "$temporary" <<'PY'
import json
import sys
from datetime import UTC, datetime
result, message, switch_id, path = sys.argv[1:]
with open(path, "w", encoding="utf-8") as output:
    json.dump(
        {
            "status": result,
            "message": message,
            "switch_id": switch_id or None,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        output,
        ensure_ascii=False,
    )
    output.write("\n")
PY
    chmod 0644 "$temporary"
    chown "$expected_uid:$expected_gid" "$temporary"
    mv -f "$temporary" "$status"
}

mkdir -p "$state_dir"
chmod 0700 "$state_dir"
exec 9>"$lock"
if ! flock -n 9; then
    write_status rejected "另一个账号切换正在执行"
    exit 0
fi

if [ ! -e "$request" ]; then
    exit 0
fi
if [ -L "$request" ] || [ ! -f "$request" ]; then
    rm -f -- "$request"
    write_status rejected "请求文件类型不安全"
    exit 0
fi
owner="$(stat -c %u "$request")"
mode="$(stat -c %a "$request")"
if [ "$owner" != "$expected_uid" ] || [ "$mode" != "$expected_mode" ]; then
    rm -f -- "$request"
    write_status rejected "请求文件所有者或权限不正确"
    exit 0
fi

consumed="$state_dir/request.$$"
mv -- "$request" "$consumed"
parsed="$(
    python3 - "$consumed" <<'PY'
import json
import re
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    value = json.load(source)
switch_id = str(value.get("switch_id") or "")
target = str(value.get("target_account_id") or "")
if not re.fullmatch(r"sw_[A-Za-z0-9_-]{8,80}", switch_id):
    raise SystemExit("invalid switch_id")
if not re.fullmatch(r"[0-9]{5,20}", target):
    raise SystemExit("invalid target account")
print(switch_id)
print(target)
PY
)" || {
    rm -f -- "$consumed"
    write_status rejected "请求内容无效"
    exit 0
}
switch_id="$(printf '%s\n' "$parsed" | sed -n '1p')"
target="$(printf '%s\n' "$parsed" | sed -n '2p')"

if [ ! -f "$env_file" ]; then
    rm -f -- "$consumed"
    write_status failed "部署 .env 不存在" "$switch_id"
    exit 1
fi

account_dir="/var/lib/qq_mcp_server/napcat/accounts/$target/qq"
install -d -m 0700 -o "$expected_uid" -g "$expected_gid" "$account_dir"
original_uid="$(stat -c %u "$env_file")"
original_gid="$(stat -c %g "$env_file")"
temporary_env="$state_dir/env.$$"
python3 - "$env_file" "$temporary_env" "$target" "$account_dir" <<'PY'
import sys
source, destination, target, account_dir = sys.argv[1:]
updates = {
    "QQ_ACCOUNT_ID": target,
    "NAPCAT_ACCOUNT_DIR": account_dir,
}
seen = set()
lines = []
with open(source, encoding="utf-8") as input_file:
    for raw in input_file:
        key = raw.split("=", 1)[0] if "=" in raw and not raw.startswith("#") else None
        if key in updates:
            lines.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            lines.append(raw)
for key, value in updates.items():
    if key not in seen:
        lines.append(f"{key}={value}\n")
with open(destination, "w", encoding="utf-8") as output:
    output.writelines(lines)
PY
chmod 0600 "$temporary_env"
chown "$original_uid:$original_gid" "$temporary_env"

cd "$deploy_dir"
compose() {
    /usr/bin/docker compose --env-file .env --env-file deploy.env "$@"
}

write_status switching "正在停止旧账号并切换固定登录目录" "$switch_id"
# 给确认页足够时间返回响应，再开始重建容器。
sleep 3
compose stop app napcat >/dev/null 2>&1 || true
mv -f "$temporary_env" "$env_file"

if ! compose run --rm --no-deps app \
    -c /config/config.toml prepare-napcat /data/napcat/config >/dev/null; then
    rm -f -- "$consumed"
    write_status failed "无法生成目标账号的只读 OneBot 配置" "$switch_id"
    exit 1
fi
if ! compose up -d --no-deps --force-recreate napcat app >/dev/null; then
    rm -f -- "$consumed"
    write_status failed "目标账号容器启动失败，保持人工处理状态" "$switch_id"
    exit 1
fi

rm -f -- "$consumed"
write_status awaiting_login "目标账号已启动，等待用户登录并通过 MCP 完成验证" "$switch_id"

