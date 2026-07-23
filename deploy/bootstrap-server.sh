#!/bin/sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
    echo "请用普通管理员用户运行，本脚本会通过 sudo 执行系统操作。" >&2
    exit 2
fi

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker.io docker-compose-v2 ca-certificates curl openssl
sudo systemctl enable --now docker
sudo usermod -aG docker "$(id -un)"

sudo install -d -m 0750 -o "$(id -un)" -g "$(id -gn)" /opt/qq_mcp_server
sudo install -d -m 0700 -o "$(id -un)" -g "$(id -gn)" \
    /var/lib/qq_mcp_server \
    /var/lib/qq_mcp_server/cards \
    /var/lib/qq_mcp_server/rules-src \
    /var/lib/qq_mcp_server/oauth \
    /var/lib/qq_mcp_server/control \
    /var/lib/qq_mcp_server/napcat/config \
    /var/lib/qq_mcp_server/napcat/qq \
    /var/lib/qq_mcp_server/caddy/data \
    /var/lib/qq_mcp_server/caddy/config

if ! swapon --show=NAME --noheadings | grep -q .; then
    if [ ! -e /swapfile ]; then
        sudo fallocate -l 2G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
    fi
    sudo swapon /swapfile
fi
if ! grep -q '^/swapfile ' /etc/fstab; then
    printf '/swapfile none swap sw 0 0\n' | sudo tee -a /etc/fstab >/dev/null
fi
printf 'vm.swappiness=10\n' | sudo tee /etc/sysctl.d/90-qq-mcp-server.conf >/dev/null
sudo sysctl --system >/dev/null

echo "✓ 服务器基础环境已准备。重新登录后可直接使用 docker。"
