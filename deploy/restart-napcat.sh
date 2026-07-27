#!/bin/sh
set -eu

request=/var/lib/qq_mcp_server/control/restart-napcat.request
status=/var/lib/qq_mcp_server/control/restart-napcat.status.json
state_dir=/var/lib/qq-mcp-recovery
last_restart="$state_dir/last-restart-epoch"
restart_history="$state_dir/restart-history"
lock="$state_dir/restart.lock"
expected_uid=1001
expected_mode=600
cooldown_seconds=3600
restart_window_seconds=86400
max_restarts_per_window=2

write_status() {
    result="$1"
    message="$2"
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    temporary="$state_dir/status.$$"
    printf '{"status":"%s","message":"%s","updated_at":"%s"}\n' \
        "$result" "$message" "$now" >"$temporary"
    chmod 0644 "$temporary"
    chown "$expected_uid:1002" "$temporary"
    mv -f "$temporary" "$status"
}

mkdir -p "$state_dir"
chmod 0700 "$state_dir"
exec 9>"$lock"
if ! flock -n 9; then
    write_status rejected "另一个恢复请求正在执行"
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
now_epoch="$(date +%s)"
previous=0
if [ -f "$last_restart" ]; then
    previous="$(cat "$last_restart")"
fi
case "$previous" in
    *[!0-9]*|"") previous=0 ;;
esac
elapsed=$((now_epoch - previous))
if [ "$elapsed" -lt "$cooldown_seconds" ]; then
    rm -f -- "$consumed"
    write_status cooldown "一小时冷却尚未结束"
    exit 0
fi

window_start=$((now_epoch - restart_window_seconds))
recent_restarts=""
if [ -f "$restart_history" ]; then
    recent_restarts="$(
        awk -v minimum="$window_start" \
            '$1 ~ /^[0-9]+$/ && $1 >= minimum { print $1 }' \
            "$restart_history"
    )"
fi
restart_count="$(printf '%s\n' "$recent_restarts" | awk 'NF { count += 1 } END { print count + 0 }')"
if [ "$restart_count" -ge "$max_restarts_per_window" ]; then
    rm -f -- "$consumed"
    write_status rejected "24 小时内最多自动恢复 2 次，现已锁定等待人工检查"
    exit 0
fi

printf '%s\n' "$now_epoch" >"$last_restart"
chmod 0600 "$last_restart"
if /usr/bin/docker restart --time 30 qq-mcp-server-napcat >/dev/null; then
    {
        [ -z "$recent_restarts" ] || printf '%s\n' "$recent_restarts"
        printf '%s\n' "$now_epoch"
    } >"$restart_history"
    chmod 0600 "$restart_history"
    rm -f -- "$consumed"
    write_status completed "NapCat 已重启"
    exit 0
fi

rm -f -- "$consumed"
write_status failed "Docker 重启命令失败"
exit 1
