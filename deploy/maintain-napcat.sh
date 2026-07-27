#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "用法：maintain-napcat.sh <initialize|update|apply-restart-policy>" >&2
    exit 2
fi

cd "$(dirname "$0")"
if [ ! -f .env ] || [ ! -f deploy.env ]; then
    echo "缺少 .env 或 deploy.env" >&2
    exit 2
fi

compose() {
    docker compose --env-file .env --env-file deploy.env "$@"
}

case "$1" in
    apply-restart-policy)
        docker update --restart=no qq-mcp-server-napcat >/dev/null
        echo "✓ NapCat 已禁用进程退出后的自动重启；容器未重启。"
        ;;
    initialize)
        if docker container inspect qq-mcp-server-napcat >/dev/null 2>&1; then
            echo "NapCat 容器已存在，拒绝执行初始化。" >&2
            exit 2
        fi
        if [ "${CONFIRM_NAPCAT_MAINTENANCE:-}" != "yes" ]; then
            echo "请显式设置 CONFIRM_NAPCAT_MAINTENANCE=yes" >&2
            exit 2
        fi
        compose pull napcat
        compose run --rm --no-deps app \
            -c /config/config.toml prepare-napcat /data/napcat/config
        compose up -d --no-deps napcat
        echo "✓ NapCat 已完成首次初始化并启动。"
        ;;
    update)
        if [ "${CONFIRM_NAPCAT_MAINTENANCE:-}" != "yes" ]; then
            echo "请显式设置 CONFIRM_NAPCAT_MAINTENANCE=yes" >&2
            exit 2
        fi
        if ! docker container inspect qq-mcp-server-napcat >/dev/null 2>&1; then
            echo "NapCat 容器不存在，请先执行 initialize。" >&2
            exit 2
        fi
        compose pull napcat
        compose run --rm --no-deps app \
            -c /config/config.toml prepare-napcat /data/napcat/config
        compose up -d --no-deps napcat
        echo "✓ NapCat 维护已执行；请检查登录与安全状态。"
        ;;
    *)
        echo "未知操作：$1" >&2
        exit 2
        ;;
esac
