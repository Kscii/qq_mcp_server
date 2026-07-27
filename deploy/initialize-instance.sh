#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "用法：initialize-instance.sh <QQ账号> <镜像>" >&2
    exit 2
fi
case "$1" in
    *[!0-9]*|"") echo "QQ 账号必须只包含数字" >&2; exit 2 ;;
esac
case "$2" in
    *[!A-Za-z0-9._/@:-]*) echo "镜像引用包含不安全字符" >&2; exit 2 ;;
esac

cd "$(dirname "$0")"
umask 077
if [ -e .env ]; then
    echo "拒绝覆盖已有 .env" >&2
    exit 2
fi

token="$(openssl rand -hex 32)"
printf '%s\n' \
    "QQ_ACCOUNT_ID=$1" \
    "NAPCAT_ACCOUNT_DIR=/var/lib/qq_mcp_server/napcat/accounts/$1/qq" \
    "HOST_UID=1001" \
    "HOST_GID=1002" \
    "DATA_DIR=/var/lib/qq_mcp_server" \
    "DATABASE_PATH=/data/trpg.sqlite3" \
    "CARD_STORAGE_DIR=/data/cards" \
    "RULES_DATABASE_PATH=/data/rules.sqlite3" \
    "OAUTH_STORAGE_DIR=/data/oauth" \
    "NAPCAT_WEBUI_CONFIG_PATH=/data/napcat/config/webui.json" \
    "NAPCAT_CONTROL_DIR=/data/control" \
    "INITIAL_COLLECTION_PAUSED=true" \
    "ONEBOT_ACCESS_TOKEN=$token" >.env
unset token
printf 'APP_IMAGE=%s\n' "$2" >deploy.env
chmod 600 .env deploy.env
echo "✓ 已创建服务器配置；秘密值未输出。"
