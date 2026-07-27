#!/bin/sh
set -eu

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "缺少部署秘密文件：$(pwd)/.env" >&2
    exit 2
fi
if [ ! -f deploy.env ]; then
    echo "缺少镜像配置：$(pwd)/deploy.env" >&2
    exit 2
fi

compose() {
    docker compose --env-file .env --env-file deploy.env "$@"
}

data_dir="$(sed -n 's/^DATA_DIR=//p' .env | tail -n 1)"
[ -n "$data_dir" ] || data_dir="/var/lib/qq_mcp_server"
case "$data_dir" in
    /*) ;;
    *) echo "DATA_DIR 必须是绝对路径" >&2; exit 2 ;;
esac
case "$data_dir" in
    *[!A-Za-z0-9._/-]*) echo "DATA_DIR 包含不安全字符" >&2; exit 2 ;;
esac
database_container_path="$(sed -n 's/^DATABASE_PATH=//p' .env | tail -n 1)"
[ -n "$database_container_path" ] || database_container_path="/data/trpg.sqlite3"
case "$database_container_path" in
    /data/*) database_path="$data_dir/${database_container_path#/data/}" ;;
    *) echo "DATABASE_PATH 必须位于 /data 持久卷内" >&2; exit 2 ;;
esac
migration_marker="$data_dir/control/pre-v4-backup.path"
profile_migrated=0

if [ "$(id -u)" -eq 0 ]; then
    ./install-recovery-helper.sh
elif ! systemctl is-enabled --quiet qq-mcp-napcat-recovery.path \
    || ! systemctl is-enabled --quiet qq-mcp-account-switch.path; then
    echo "缺少 NapCat 恢复/切号助手；请先以 root 运行 install-recovery-helper.sh" >&2
    exit 2
fi

registry="australia-southeast1-docker.pkg.dev"
access_token="$(
    curl -fsS -H 'Metadata-Flavor: Google' \
        'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
)"
printf '%s' "$access_token" | docker login -u oauth2accesstoken \
    --password-stdin "$registry" >/dev/null
unset access_token
trap 'docker logout "$registry" >/dev/null 2>&1 || true' EXIT

app_image="$(sed -n 's/^APP_IMAGE=//p' deploy.env | head -n 1)"
if [ -z "$app_image" ]; then
    echo "deploy.env 中缺少 APP_IMAGE" >&2
    exit 2
fi
collector_image="$(sed -n 's/^COLLECTOR_IMAGE=//p' deploy.env | head -n 1)"
if [ -z "$collector_image" ]; then
    echo "deploy.env 中缺少 COLLECTOR_IMAGE" >&2
    exit 2
fi
if ! docker image inspect "$app_image" >/dev/null 2>&1; then
    docker pull "$app_image"
fi
if ! docker image inspect "$collector_image" >/dev/null 2>&1; then
    docker pull "$collector_image"
fi
schema_version="$(
    python3 -c '
import os, sqlite3, sys
path = sys.argv[1]
if not os.path.isfile(path):
    print("")
else:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM app_metadata WHERE key = '\''schema_version'\''"
        ).fetchone()
        print(row[0] if row else "")
    except sqlite3.Error:
        print("")
    finally:
        connection.close()
' "$database_path"
)"
if { [ "$schema_version" = "2" ] || [ "$schema_version" = "3" ]; } \
    && [ "${SKIP_SAFETY_MIGRATION:-0}" != "1" ]; then
    # 旧进程不会动态读取新写入的暂停状态，所以迁移前只停止应用。
    # NapCat 的容器、登录目录和会话均不在应用发布链中。
    compose stop app >/dev/null 2>&1 || true
    backup_output="$(
        compose run --rm --no-deps app -c /config/config.toml backup \
            --output-dir /data/backups
    )"
    printf '%s\n' "$backup_output"
    backup_container_path="$(
        printf '%s\n' "$backup_output" |
            sed -n 's/^✓ 数据库备份：//p' |
            tail -n 1
    )"
    case "$backup_container_path" in
        /data/backups/*)
            backup_path="$data_dir/${backup_container_path#/data/}"
            ;;
        *)
            echo "无法确认 v2/v3 数据库备份路径，拒绝迁移" >&2
            exit 1
            ;;
    esac
    if [ ! -f "$backup_path" ]; then
        echo "v2/v3 数据库备份不存在：$backup_path" >&2
        exit 1
    fi
    control_dir="$(dirname "$migration_marker")"
    if [ ! -d "$control_dir" ]; then
        install -d -m 0750 \
            -o "$(stat -c %u "$data_dir")" \
            -g "$(stat -c %g "$data_dir")" \
            "$control_dir"
    fi
    printf '%s\n' "$backup_path" >"$migration_marker"
    chmod 0600 "$migration_marker"
    compose run --rm --no-deps app -c /config/config.toml pause-collection \
        --reason "v0.6 纯 SSE 与多账号升级：等待人工确认账号后恢复"
    migrated_schema="$(
        python3 -c '
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
try:
    row = connection.execute(
        "SELECT value FROM app_metadata WHERE key = '\''schema_version'\''"
    ).fetchone()
    print(row[0] if row else "")
finally:
    connection.close()
' "$database_path"
    )"
    if [ "$migrated_schema" != "4" ]; then
        echo "数据库未完成 v4 迁移，拒绝启动新应用" >&2
        exit 1
    fi
    compose run --rm --no-deps app -c /config/config.toml status --json >/dev/null
fi

if ! grep -q '^NAPCAT_ACCOUNT_DIR=.' .env; then
    current_account="$(sed -n 's/^QQ_ACCOUNT_ID=//p' .env | tail -n 1)"
    case "$current_account" in
        *[!0-9]*|"") echo "QQ_ACCOUNT_ID 格式无效，拒绝迁移登录目录" >&2; exit 2 ;;
    esac
    old_account_dir="$data_dir/napcat/qq"
    new_account_dir="$data_dir/napcat/accounts/$current_account/qq"
    compose stop app napcat >/dev/null 2>&1 || true
    install -d -m 0700 \
        -o "$(stat -c %u "$data_dir")" \
        -g "$(stat -c %g "$data_dir")" \
        "$new_account_dir"
    if [ -d "$old_account_dir" ] && [ -z "$(find "$new_account_dir" -mindepth 1 -print -quit)" ]; then
        cp -a "$old_account_dir/." "$new_account_dir/"
    fi
    temporary_env=".env.account-migration.$$"
    python3 - "$new_account_dir" .env "$temporary_env" <<'PY'
import sys
account_dir, source, destination = sys.argv[1:]
with open(source, encoding="utf-8") as input_file:
    lines = input_file.readlines()
lines.append(f"NAPCAT_ACCOUNT_DIR={account_dir}\n")
with open(destination, "w", encoding="utf-8") as output:
    output.writelines(lines)
PY
    chmod 0600 "$temporary_env"
    chown "$(stat -c %u .env):$(stat -c %g .env)" "$temporary_env"
    mv -f "$temporary_env" .env
    profile_migrated=1
fi

collector_first_install=0
if ! docker container inspect qq-mcp-server-collector >/dev/null 2>&1; then
    collector_first_install=1
fi
deploy_collector="${DEPLOY_COLLECTOR:-0}"
case "$deploy_collector" in
    0|1) ;;
    *) echo "DEPLOY_COLLECTOR 只能是 0 或 1" >&2; exit 2 ;;
esac

if [ "$collector_first_install" -eq 1 ]; then
    # 一次性从 HTTP-SSE 迁移到反向 WebSocket。先写新配置，再停止占用
    # 3001 端口的 NapCat，启动采集器，最后只启动 NapCat 一次。
    compose run --rm --no-deps app \
        -c /config/config.toml prepare-napcat /data/napcat/config
    compose stop napcat >/dev/null 2>&1 || true
    docker update --restart=no qq-mcp-server-napcat >/dev/null 2>&1 || true
    compose up -d --no-deps collector
    compose up -d --no-deps napcat
elif [ "$deploy_collector" = "1" ]; then
    compose up -d --no-deps collector
fi

if grep -q '^PUBLIC_DOMAIN=.' .env; then
    compose --profile public pull caddy
    if [ "$profile_migrated" -eq 1 ]; then
        compose --profile public up -d --no-deps napcat
    fi
    compose --profile public up -d --no-deps app caddy
else
    if [ "$profile_migrated" -eq 1 ]; then
        compose up -d --no-deps napcat
    fi
    compose up -d --no-deps app
fi

attempt=0
while [ "$attempt" -lt 18 ]; do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' qq-mcp-server-app 2>/dev/null || true)"
    collector_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' qq-mcp-server-collector 2>/dev/null || true)"
    if [ "$health" = "healthy" ] && [ "$collector_health" = "healthy" ]; then
        rm -f "$migration_marker"
        compose ps
        exit 0
    fi
    if [ "$health" = "unhealthy" ] || [ "$collector_health" = "unhealthy" ]; then
        docker logs --tail 100 qq-mcp-server-app >&2 || true
        docker logs --tail 100 qq-mcp-server-collector >&2 || true
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 5
done

docker logs --tail 100 qq-mcp-server-app >&2 || true
docker logs --tail 100 qq-mcp-server-collector >&2 || true
echo "服务在 90 秒内未通过健康检查" >&2
exit 1
