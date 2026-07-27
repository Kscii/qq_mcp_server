#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "用法：deploy-image.sh <不可变镜像引用>" >&2
    exit 2
fi

case "$1" in
    australia-southeast1-docker.pkg.dev/*@sha256:*) ;;
    *) echo "拒绝非悉尼 Artifact Registry digest 镜像：$1" >&2; exit 2 ;;
esac

cd "$(dirname "$0")"
old_image=""
old_collector_image=""
if [ -f deploy.env ]; then
    old_image="$(sed -n 's/^APP_IMAGE=//p' deploy.env | head -n 1)"
    old_collector_image="$(sed -n 's/^COLLECTOR_IMAGE=//p' deploy.env | head -n 1)"
fi
deploy_collector="${DEPLOY_COLLECTOR:-0}"
case "$deploy_collector" in
    0|1) ;;
    *) echo "DEPLOY_COLLECTOR 只能是 0 或 1" >&2; exit 2 ;;
esac
if [ "$deploy_collector" = "1" ] || [ -z "$old_collector_image" ]; then
    collector_image="$1"
else
    collector_image="$old_collector_image"
fi
data_dir="$(sed -n 's/^DATA_DIR=//p' .env | tail -n 1)"
[ -n "$data_dir" ] || data_dir="/var/lib/qq_mcp_server"
database_container_path="$(sed -n 's/^DATABASE_PATH=//p' .env | tail -n 1)"
[ -n "$database_container_path" ] || database_container_path="/data/trpg.sqlite3"
case "$database_container_path" in
    /data/*) database_path="$data_dir/${database_container_path#/data/}" ;;
    *) echo "DATABASE_PATH 必须位于 /data 持久卷内" >&2; exit 2 ;;
esac
migration_marker="$data_dir/control/pre-v4-backup.path"
printf 'APP_IMAGE=%s\nCOLLECTOR_IMAGE=%s\n' "$1" "$collector_image" > deploy.env
chmod 600 deploy.env

if ./deploy.sh; then
    exit 0
fi

if [ -n "$old_image" ]; then
    if [ -f "$migration_marker" ]; then
        backup_path="$(sed -n '1p' "$migration_marker")"
        case "$backup_path" in
            "$data_dir"/backups/*) ;;
            *)
                echo "迁移备份路径不在 DATA_DIR/backups 内，拒绝自动恢复" >&2
                exit 1
                ;;
        esac
        if [ ! -f "$backup_path" ]; then
            echo "迁移备份不存在，拒绝自动恢复：$backup_path" >&2
            exit 1
        fi
        echo "新版本健康检查失败，正在恢复迁移前数据库。" >&2
        docker compose --env-file .env --env-file deploy.env stop app >/dev/null 2>&1 || true
        python3 -c '
import os, sqlite3, sys

source_path, destination_path = sys.argv[1:3]
temporary_path = destination_path + ".restore"
try:
    os.unlink(temporary_path)
except FileNotFoundError:
    pass
original = os.stat(destination_path) if os.path.exists(destination_path) else None
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
destination = sqlite3.connect(temporary_path)
try:
    source.backup(destination)
    result = destination.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("恢复后的数据库完整性检查失败")
finally:
    destination.close()
    source.close()
if original is not None:
    os.chown(temporary_path, original.st_uid, original.st_gid)
    os.chmod(temporary_path, original.st_mode & 0o777)
for suffix in ("-wal", "-shm"):
    try:
        os.unlink(destination_path + suffix)
    except FileNotFoundError:
        pass
os.replace(temporary_path, destination_path)
' "$backup_path" "$database_path"
    fi
    echo "恢复上一镜像。" >&2
    if [ -n "$old_collector_image" ]; then
        rollback_collector_image="$old_collector_image"
    else
        rollback_collector_image="$collector_image"
        docker compose --env-file .env --env-file deploy.env stop collector \
            >/dev/null 2>&1 || true
    fi
    printf 'APP_IMAGE=%s\nCOLLECTOR_IMAGE=%s\n' \
        "$old_image" "$rollback_collector_image" > deploy.env
    chmod 600 deploy.env
    DEPLOY_COLLECTOR=0 SKIP_SAFETY_MIGRATION=1 ./deploy.sh
fi
exit 1
