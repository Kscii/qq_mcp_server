# Google Cloud 部署

生产设计是一台 Ubuntu VM、Docker Compose，以及 NapCat、应用和可选 Caddy 三个容器。
NapCat 与应用使用 host network，但分别只监听 `127.0.0.1:3000/6099` 和
`127.0.0.1:8000`；Caddy 是唯一公网入口。建议 2 vCPU、4 GB 内存、30 GB 磁盘和
2 GB swap。

## 一次性准备

将 `deploy/bootstrap-server.sh` 复制到服务器并以普通 sudo 用户运行。它安装
Docker/Compose，创建权限收紧的 `/var/lib/qq_mcp_server`、`cards`、`rules-src`、
`oauth` 等目录并配置 swap。

进入 `/opt/qq_mcp_server/deploy`，用不可变镜像引用初始化：

```bash
./initialize-instance.sh 123456789 \
  australia-southeast1-docker.pkg.dev/PROJECT/REPOSITORY/qq-mcp-server@sha256:DIGEST
```

脚本生成权限为 `0600` 的 `.env` 与 `deploy.env`。`.env` 只包含 QQ 账号、持久化路径和
OneBot token，不再包含目标群；群在服务运行后通过管理 App 白名单加入。

首次启动 NapCat 后，在本机建立 SSH 转发：

```bash
gcloud compute ssh qq-mcp-server --zone australia-southeast1-a \
  -- -L 6099:127.0.0.1:6099
```

打开 `http://127.0.0.1:6099/webui` 扫码。QQ 登录目录在持久卷，正常重启无需重新扫码；
QQ 仍可能因设备授权或风控要求重新授权。不要在部署配置中保存主用 QQ 明文密码。

## 离线构建规则索引

将三本合法持有的 PDF 临时复制到服务器的私有目录：

```text
/var/lib/qq_mcp_server/rules-src/investigator.pdf
/var/lib/qq_mcp_server/rules-src/keeper.pdf
/var/lib/qq_mcp_server/rules-src/magic.pdf
```

目录属于应用 UID/GID 且不可公开。执行：

```bash
docker compose --env-file .env --env-file deploy.env run --rm --no-deps \
  --entrypoint qq_mcp_server app -c /config/config.toml build-rules \
  --investigator /data/rules-src/investigator.pdf \
  --keeper /data/rules-src/keeper.pdf \
  --magic /data/rules-src/magic.pdf
```

构建会把 `/data/rules.sqlite3` 原子替换为三书文本索引。索引记录来源哈希和 PDF 页码，
不包含 PDF 文件本身；服务运行期没有 PDF 上传入口。先用 `status --json` 确认
`rules.ready=true`，再启用群。

## 域名与 OAuth

DNS A 记录指向 VM 静态 IP。在 `.env` 增加：

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

Google OAuth Web Application 回调地址必须精确填写
`https://qq-mcp.example.com/auth/callback`。两个 MCP App 共用同一 OAuth 提供方和完整
邮箱白名单。Caddy 自动申请证书；不要公开 3000、6099 或 8000 端口。

连接 ChatGPT 时先添加 `https://qq-mcp.example.com/mcp/admin`。通过管理 App 加群并完成
配置后，`admin.list_groups` 会返回每个群唯一的
`https://qq-mcp.example.com/mcp/groups/{group_key}`，把它作为独立 App 连接到对应跑团对话。

## GitHub Actions 部署身份

发布工作流使用 GitHub OIDC 临时扮演部署服务账号，不保存 Google Cloud 长期密钥。首次
启用自动部署时，用拥有项目 IAM 管理权限、且当前仍能 SSH 到 VM 的管理员执行以下命令。
先给管理员本人和 GitHub 部署账号配置 OS Login，再切换实例；不要颠倒顺序，否则可能暂时
失去 SSH 登录能力。

```bash
PROJECT_ID=project-51b589c7-8d5e-4e78-a10
ZONE=australia-southeast1-a
INSTANCE=qq-mcp-server
ADMIN_EMAIL=你的_Google_账号
DEPLOY_SA=github-qq-mcp-deployer@project-51b589c7-8d5e-4e78-a10.iam.gserviceaccount.com
VM_SA=qq-mcp-server-vm@project-51b589c7-8d5e-4e78-a10.iam.gserviceaccount.com

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$ADMIN_EMAIL" --role=roles/compute.osAdminLogin
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$ADMIN_EMAIL" --role=roles/iap.tunnelResourceAccessor

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOY_SA" --role=roles/compute.osAdminLogin
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOY_SA" --role=roles/iap.tunnelResourceAccessor
gcloud iam service-accounts add-iam-policy-binding "$VM_SA" \
  --member="serviceAccount:$DEPLOY_SA" --role=roles/iam.serviceAccountUser

gcloud compute instances add-metadata "$INSTANCE" \
  --project="$PROJECT_ID" --zone="$ZONE" --metadata=enable-oslogin=TRUE
gcloud compute ssh "$INSTANCE" \
  --project="$PROJECT_ID" --zone="$ZONE" --tunnel-through-iap --command=true
```

项目中还需要允许 IAP 地址段 `35.235.240.0/20` 入站访问 VM 的 TCP 22；若管理员使用
`--tunnel-through-iap` 已经能连接，则这条防火墙规则通常已经存在。部署账号原有的
`roles/compute.viewer` 和 Artifact Registry Writer 仍需保留。`roles/compute.osAdminLogin`
提供工作流中 `sudo` 所需权限，`roles/iam.serviceAccountUser` 只授予在 VM 所绑定服务账号
上的使用权。

## 发布与回滚

推送 `vX.Y.Z` 形式且与 `pyproject.toml` 版本一致的标签后，GitHub Actions 会：

1. 执行格式、类型和测试检查。
2. 构建 wheel 并创建 GitHub Release。
3. 通过 GitHub OIDC 获取短期 Google 凭证。
4. 构建镜像并推送到 Artifact Registry。
5. 以镜像 digest 部署；健康检查失败则恢复上一 digest。

普通 `main` 推送只运行 CI。`deploy.sh` 会准备安全 NapCat 配置并启动容器；应用健康检查
调用 `status --json`，不要求 QQ 当时在线。SQLite、人物卡、规则索引和 OAuth 状态都在
`DATA_DIR` 持久卷中。

v0.2 是不兼容的新数据模型。首次部署请使用新的 `/data/trpg.sqlite3`，不要覆盖或导入
旧 qq_mcp_server/Dice Echo 数据；所有群重新加入白名单，人物卡重新上传。
