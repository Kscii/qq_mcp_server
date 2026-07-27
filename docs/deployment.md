# Google Cloud 部署

当前生产设计是一台 Ubuntu VM、Docker Compose，以及 NapCat、collector、API 和可选
Caddy 四个容器。它们使用 host network，但只监听回环地址：OneBot HTTP 3000、反向
WebSocket collector 3001、NapCat WebUI 6099、API 8000；Caddy 是唯一公网入口。
建议 2 vCPU、4 GB 内存、30 GB 磁盘和 2 GB swap。

API、collector 与 QQ 客户端生命周期分离：普通 API 部署不更新 collector，也不拉取、
更新或重启 NapCat。首次启用反向 WebSocket collector 时是一次性例外：部署先写入
NapCat 配置，停止 NapCat 一次，启动 collector，再启动 NapCat 一次。之后只有 collector
相关源文件变化才替换 collector，仍不会重启 NapCat。

## 一次性准备

将 `deploy/bootstrap-server.sh` 复制到服务器并以普通 sudo 用户运行。它安装
Docker/Compose，创建权限收紧的 `/var/lib/qq_mcp_server`、`cards`、`rules-src`、
`oauth` 等目录并配置 swap。

进入 `/opt/qq_mcp_server/deploy`，用不可变镜像引用初始化：

```bash
./initialize-instance.sh 123456789 \
  australia-southeast1-docker.pkg.dev/PROJECT/REPOSITORY/qq-mcp-server@sha256:DIGEST
```

脚本生成权限为 `0600` 的 `.env` 与 `deploy.env`。`.env` 只包含当前 QQ、账号专属
`NAPCAT_ACCOUNT_DIR`、持久化路径和 OneBot token，不包含目标群；群通过管理 App 授权。首次配置默认
`INITIAL_COLLECTION_PAUSED=true`。

NapCat 首次初始化必须单独明确确认：

```bash
CONFIRM_NAPCAT_MAINTENANCE=yes ./maintain-napcat.sh initialize
```

后续只有主动升级 NapCat 时才运行
`CONFIRM_NAPCAT_MAINTENANCE=yes ./maintain-napcat.sh update`。普通应用发布不调用这两个
动作。若只需在线收紧已有容器的重启策略，执行：

```bash
./maintain-napcat.sh apply-restart-policy
```

该动作把 NapCat 的 Docker 重启策略设为 `no`，不会当场重启容器。QQ 下线后必须人工
判断并登录，系统不会通过自动重启制造重复登录。

首次启动时可以先用 SSH 转发打开 NapCat：

```bash
gcloud compute ssh qq-mcp-server --zone australia-southeast1-a \
  -- -L 6099:127.0.0.1:6099
```

打开 `http://127.0.0.1:6099/webui` 扫码。QQ 登录目录在持久卷，正常重启无需重新扫码；
QQ 仍可能因设备授权或风控要求重新授权。不要在部署配置中保存主用 QQ 明文密码。确认
账号无误后等待状态页显示稳定恢复。会话离线后，collector 会观察五分钟、连续确认两次
并校验账号，随后只补一页历史；不要反复点登录或重启 NapCat。人工维护暂停则仍由管理
MCP 调用 `admin.resume_qq_collection`。账号切换使用登记/切换流程，不要直接改 `.env`。

## Tailscale 私有 NapCat 面板

NapCat 仍只监听 `127.0.0.1:6099`。电脑、iOS 和 VM 加入同一个 Tailnet；不要启用
Tailscale Funnel。Arch 使用 pacman 安装并启动：

```bash
sudo pacman -S tailscale
sudo systemctl enable --now tailscaled
sudo tailscale up
```

iOS 安装 Tailscale App 并登录同一账号。VM 按 Tailscale 官方 Ubuntu 软件源安装后执行：

```bash
sudo systemctl enable --now tailscaled
sudo tailscale up
sudo tailscale serve --bg --https=8443 http://127.0.0.1:6099
tailscale serve status
```

在 Tailnet ACL/grants 中只允许你的身份访问该 VM 的 TCP 8443。然后在服务器 `.env` 增加：

```text
NAPCAT_WEBUI_URL=https://VM-MAGICDNS-NAME.TAILNET.ts.net:8443/webui
NAPCAT_WEBUI_CONFIG_PATH=/data/napcat/config/webui.json
NAPCAT_CONTROL_DIR=/data/control
```

`admin.open_napcat_webui` 只返回公网项目域名下十分钟有效的一次性入口。浏览器确认后，
服务器读取当前 `webui.json`，再 303 跳转到带 Token 的 Tailnet URL。Token 不出现在
MCP 返回值；最终页面也只有已登录 Tailnet 的设备能够访问。

部署会安装 root 所有的 `qq-mcp-napcat-recovery.path/service` 和
`qq-mcp-account-switch.path/service`。应用没有 Docker Socket，
只能写入固定 `restart-napcat.request`；助手校验 UID、0600 权限、一小时冷却和 24 小时
最多两次的预算后，只执行 `docker restart --time 30 qq-mcp-server-napcat`。登录失效、
账号不匹配或互踢不会自动触发恢复入口，而会持久化暂停等待人工处理。不要把这个助手改成
接受容器名或命令参数。切号助手只接受固定 JSON 请求，目标必须是已登记纯数字 QQ；
它更新固定 `.env` 字段、选择账号专属登录目录并只重建一个 NapCat 和应用容器。

## 账号风控期间的安全发布

账号被冻结或待人工重新登录时：

1. NapCat 若仍在运行，只人工停止一次，不再反复启动验证。
2. 保持 `INITIAL_COLLECTION_PAUSED=true`；不要调用恢复采集接口。
3. 发布新应用。首次 v2/v3→v4 升级会先停止旧应用，使用 SQLite 在线备份并校验，再写入
   持久化暂停状态；数据库迁移本身不会连接 OneBot。
4. v0.6 首次账号目录迁移会停止 NapCat 一次；若账号仍冻结，不要在部署后恢复采集。
5. 新应用健康检查失败时，发布脚本先原子恢复迁移前备份，再回滚旧镜像。
6. 账号解冻并在固定设备完成登录后，先看缓存状态，等待五分钟稳定观察与一次自动补页。

应用健康检查只打开数据库和规则索引，不探测 QQ，因此冻结期间也能安全完成部署。

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

还应把 VM 当前出口地址提升为区域静态地址，避免停止/启动后 QQ 登录 IP 无故变化。若
当前外部地址仍是 `35.189.4.24`，可在不重启 VM 的情况下保留它：

```bash
gcloud compute addresses create qq-mcp-server-egress \
  --project=project-51b589c7-8d5e-4e78-a10 \
  --region=australia-southeast1 \
  --addresses=35.189.4.24
gcloud compute addresses describe qq-mcp-server-egress \
  --project=project-51b589c7-8d5e-4e78-a10 \
  --region=australia-southeast1
```

执行前必须先核对实例当前外部 IP；地址不同就使用实际值，不要强行申请旧地址。固定出口
只能减少 IP 漂移，不能消除非官方 QQ 客户端自身的账号风险。

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
5. 以镜像 digest 部署 API；仅当 collector 相关文件变化时同步更新 collector。
6. API 或 collector 健康检查失败则回滚镜像；发布过程不会自动重启 NapCat。

普通 `main` 推送只运行 CI。`deploy.sh` 更新 API、按需更新 collector 和可选 Caddy；
健康检查不要求 QQ 当时在线。SQLite、人物卡、规则索引和 OAuth 状态都在 `DATA_DIR`
持久卷中。

v0.6 首次发布会把本项目 schema v2/v3 原子升级为 v4，并保留迁移前备份；不需要
重新授权群或上传人物卡。它同时启用消息缺口、OneBot 调用审计和多 QQ 账号表。
旧 qq_mcp_server/Dice Echo 数据仍不支持迁移。

## 切换备用 QQ

生产环境不要直接修改 `QQ_ACCOUNT_ID` 或复制登录文件。通过管理 MCP：

1. `admin.open_qq_account_registration` 登记目标 QQ。
2. `admin.begin_qq_account_switch` 取得浏览器确认页。
3. 确认后等待约 20 秒，打开 `admin.open_napcat_webui` 返回的私有入口登录目标账号。
4. 登录完成后保持五分钟，不要重复登录；再调用 `admin.complete_qq_account_switch`。

每个账号登录目录位于
`/var/lib/qq_mcp_server/napcat/accounts/<QQ>/qq`；SQLite、cards、rules 和 oauth 不随
账号切换。失败不自动回滚，以免旧、新账号反复互踢。

## 未来将 NapCat 移到住宅网络

公网 MCP、OAuth、Caddy 和 SQLite 默认继续留在 GCP。到澳洲后先保持这套部署观察至少
30 天；若账号仍有明显地域风险，再只把 NapCat 移到长期稳定的住宅设备。反向
WebSocket 不能直接跨 NAT 连接 GCP 回环 collector，因此迁移前需要增加受 Tailscale ACL
保护的私网 collector 地址，或在住宅端运行转发代理；不能只修改旧的
`onebot_sse_url`。该扩展尚未实现。

切换时必须先暂停采集并确保旧 NapCat 已停止，再启动新位置；任何时候都不得让两个
NapCat 实例同时登录同一 QQ。此方案只改变 QQ 客户端位置，不改变 ChatGPT 访问公网 MCP
的路径。
