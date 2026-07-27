#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "install-recovery-helper.sh 必须由 root 执行" >&2
    exit 2
fi

cd "$(dirname "$0")"
install -d -m 0755 /usr/local/libexec
install -m 0755 restart-napcat.sh /usr/local/libexec/qq-mcp-server-restart-napcat
install -m 0755 switch-napcat-account.sh \
    /usr/local/libexec/qq-mcp-server-switch-napcat-account
install -m 0644 qq-mcp-napcat-recovery.service \
    /etc/systemd/system/qq-mcp-napcat-recovery.service
install -m 0644 qq-mcp-napcat-recovery.path \
    /etc/systemd/system/qq-mcp-napcat-recovery.path
install -m 0644 qq-mcp-account-switch.service \
    /etc/systemd/system/qq-mcp-account-switch.service
install -m 0644 qq-mcp-account-switch.path \
    /etc/systemd/system/qq-mcp-account-switch.path
install -d -m 0700 -o 1001 -g 1002 /var/lib/qq_mcp_server/control
install -d -m 0700 -o root -g root /var/lib/qq-mcp-recovery
install -d -m 0700 -o root -g root /var/lib/qq-mcp-account-switch
systemctl daemon-reload
systemctl enable --now qq-mcp-napcat-recovery.path
systemctl enable --now qq-mcp-account-switch.path
