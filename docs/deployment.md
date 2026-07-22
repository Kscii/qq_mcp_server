# Google Cloud 部署

当前生产设计是一台 Ubuntu VM、Docker Compose 和三个容器：NapCat、应用、可选
Caddy。NapCat 与应用使用 host network，但只监听 `127.0.0.1`；Caddy 是唯一公网
入口。

## 一次性准备

将 `deploy/bootstrap-server.sh` 复制到服务器并以普通 sudo 用户运行。脚本会安装
Ubuntu 自带 Docker/Compose、建立 `/var/lib/qq_mcp_server`、启用 Docker，并创建
2 GB swap。

在 `/opt/qq_mcp_server/.env` 写入部署配置，权限设为 `0600`。以
`deploy/server.env.example` 为模板，不要提交该文件。

首次启动 NapCat 后，在本机建立 SSH 转发：

```bash
gcloud compute ssh qq-mcp-server --zone australia-southeast1-a \
  -- -L 6099:127.0.0.1:6099
```

打开 `http://127.0.0.1:6099/webui` 扫码。QQ 登录目录位于持久卷，正常重启不需要
重新扫码；但 QQ 仍可能因设备授权、风控或登录态失效要求重新授权。程序会在
每个轮询周期重试，重新登录后无需手动重启应用。不建议把主用 QQ 的明文密码
写入部署配置。

## 发布

推送 `v0.1.0` 形式且与 `pyproject.toml` 版本一致的标签后，GitHub Actions 会：

1. 执行格式、类型和测试检查。
2. 构建 wheel 并创建 GitHub Release。
3. 通过 GitHub OIDC 获取短期 Google 凭证。
4. 构建镜像并推送到 Artifact Registry。
5. 以镜像 digest 部署；健康检查失败则恢复上一 digest。

普通 `main` 推送只运行 CI，不会部署。

## 域名与 OAuth

DNS 的 A 记录指向 VM 静态 IP。在 `.env` 中增加：

```text
PUBLIC_DOMAIN=qq-mcp.example.com
PUBLIC_URL=https://qq-mcp.example.com
ACME_EMAIL=you@example.com
ALLOWED_GOOGLE_EMAIL=you@gmail.com
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
MCP_JWT_SIGNING_KEY=...
MCP_STORAGE_ENCRYPTION_KEY=...
```

Google OAuth Web Application 的回调地址必须精确填写
`https://qq-mcp.example.com/auth/callback`。Caddy 会自动申请和续期证书。不要公开
3000、6099、8000 端口。
