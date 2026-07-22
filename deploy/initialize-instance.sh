#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
    echo "用法：initialize-instance.sh <QQ账号> <群号> <群名> <镜像>" >&2
    exit 2
fi
case "$1:$2" in
    *[!0-9:]*|:*|*:) echo "QQ 账号和群号必须只包含数字" >&2; exit 2 ;;
esac
case "$3" in
    ""|*[!A-Za-z0-9._-]*) echo "初始化群名只能使用字母、数字、点、下划线和连字符" >&2; exit 2 ;;
esac
case "$4" in
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
    "QQ_GROUP_ID=$2" \
    "QQ_GROUP_NAME=$3" \
    "HOST_UID=1001" \
    "HOST_GID=1002" \
    "DATA_DIR=/var/lib/qq_mcp_server" \
    "DATABASE_PATH=/data/messages.sqlite3" \
    "EXPORT_PATH=/data/groups/$2.txt" \
    "OAUTH_STORAGE_DIR=/data/oauth" \
    "ONEBOT_ACCESS_TOKEN=$token" >.env
unset token
printf 'APP_IMAGE=%s\n' "$4" >deploy.env
chmod 600 .env deploy.env
echo "✓ 已创建服务器配置；秘密值未输出。"
