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
if [ -f deploy.env ]; then
    old_image="$(sed -n 's/^APP_IMAGE=//p' deploy.env | head -n 1)"
fi
printf 'APP_IMAGE=%s\n' "$1" > deploy.env
chmod 600 deploy.env

if ./deploy.sh; then
    exit 0
fi

if [ -n "$old_image" ]; then
    echo "新版本健康检查失败，恢复上一镜像。" >&2
    printf 'APP_IMAGE=%s\n' "$old_image" > deploy.env
    chmod 600 deploy.env
    ./deploy.sh
fi
exit 1
