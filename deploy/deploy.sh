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

if [ "$(id -u)" -eq 0 ]; then
    ./install-recovery-helper.sh
elif ! systemctl is-enabled --quiet qq-mcp-napcat-recovery.path; then
    echo "缺少 NapCat 恢复助手；请先以 root 运行 install-recovery-helper.sh" >&2
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
if ! docker image inspect "$app_image" >/dev/null 2>&1; then
    docker pull "$app_image"
fi
compose pull napcat
compose run --rm --no-deps --entrypoint qq_mcp_server app \
    -c /config/config.toml prepare-napcat /data/napcat/config

if grep -q '^PUBLIC_DOMAIN=.' .env; then
    compose --profile public pull caddy
    compose --profile public up -d --remove-orphans
else
    compose up -d napcat app --remove-orphans
fi

attempt=0
while [ "$attempt" -lt 18 ]; do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' qq-mcp-server-app 2>/dev/null || true)"
    if [ "$health" = "healthy" ]; then
        compose ps
        exit 0
    fi
    if [ "$health" = "unhealthy" ]; then
        docker logs --tail 100 qq-mcp-server-app >&2 || true
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 5
done

docker logs --tail 100 qq-mcp-server-app >&2 || true
echo "服务在 90 秒内未通过健康检查" >&2
exit 1
