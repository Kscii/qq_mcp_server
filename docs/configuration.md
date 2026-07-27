# 配置与操作

## 配置归属

| 内容 | 入口 |
|---|---|
| 当前 QQ、OneBot 地址、存储路径、OAuth、公网地址 | TOML、环境变量和部署脚本 |
| 登记备用 QQ | `admin.open_qq_account_registration` 的一次性网页 |
| 切换 QQ | `admin.begin_qq_account_switch` 的确认页，登录后由 MCP 完成验证 |
| 允许/禁止 AI 读取群 | `admin.open_group_access` 的一次性网页 |
| 模组名、显示名、长期 RP 准则（最多 16000 字） | `admin.update_group_profile` |
| 玩家 QQ 别名、KP、骰娘 | `admin.list_group_members` 后 `admin.set_member_roles` |
| 跑团启用/停用 | `admin.set_group_enabled` |
| 当前 Excel 人物卡 | `trpg.begin_character_card_upload` 的上传、预览和确认页 |
| 人物卡动态值与团务笔记 | `trpg.commit_turn_updates` |
| 查看单群全部已保存资料 | `trpg.open_campaign_dashboard` 的一小时只读页 |
| 消息缺口 | 管理 MCP 列出、登记、人工启动修复或接受 |
| 三本规则书 | 部署者执行 `build-rules` |

WebUI 刻意没有通用设置页。它只承担群访问授权、账号登记/切换确认、文件选择、
NapCat 私有跳转和只读模组面板；资料修改尽量留在带结构化参数和版本检查的 MCP。

## 命令

```bash
qq_mcp_server setup
qq_mcp_server build-rules --investigator INVESTIGATOR.pdf --keeper KEEPER.pdf --magic MAGIC.pdf
qq_mcp_server run
qq_mcp_server status [--json]
qq_mcp_server backup
qq_mcp_server pause-collection --reason "维护"
qq_mcp_server prepare-napcat DIRECTORY
```

`status`、`backup` 和 `pause-collection` 不连接 QQ。v0.6 不再提供主动 `sync` 命令；
正常采集只消费 SSE，历史只通过消息缺口流程人工启动。

## TOML 与环境变量

示例见 [`config.example.toml`](../config.example.toml)。群号、模组、账号登记和切换状态
都不属于静态 TOML。

`[qq]` 的有效运行参数：

- `account_id` / `QQ_ACCOUNT_ID`：当前唯一运行的 NapCat QQ。
- `onebot_url` / `ONEBOT_URL`：回环 HTTP 或 Tailnet `.ts.net` HTTPS。
- `onebot_sse_url` / `ONEBOT_SSE_URL`：同上，路径必须为 `/_events`。
- `page_size`：人工缺口修复的历史分页大小。
- `initial_collection_paused` / `INITIAL_COLLECTION_PAUSED`：首次创建控制状态时是否暂停。
- `request_timeout_seconds` / `history_timeout_seconds`：普通/历史接口超时。

`poll_interval_seconds`、`registry_refresh_seconds`、`group_discovery_interval_seconds`、
`context_freshness_seconds`、`sync_concurrency`、`backfill_*`、`unreachable_backoff_max_seconds`
和 `history_since` 仍可读取旧配置，但 v0.6 的常驻服务不再运行周期历史/群列表调度器。

`[storage]`：

- `database` / `DATABASE_PATH`：共享 SQLite；切号不会更换。
- `cards` / `CARD_STORAGE_DIR`：共享人物卡和上传暂存目录。
- `rules` / `RULES_DATABASE_PATH`：共享三书索引。
- `timezone`：消息显示使用的 IANA 时区。
- `oauth` / `OAUTH_STORAGE_DIR`：OAuth 持久化状态。

`[server]`：

- `host`、`port` / `PORT`：应用监听地址和端口。
- `public_url` / `PUBLIC_URL`：公网 HTTPS 根地址；设置后要求 OAuth 与邮箱授权。
- `upload_token_ttl_seconds`：一次性网页链接时效。
- `napcat_webui_url` / `NAPCAT_WEBUI_URL`：Tailscale 私有
  `https://<设备>.ts.net:8443/webui`。
- `napcat_webui_config` / `NAPCAT_WEBUI_CONFIG_PATH`：跳转确认时读取当前 WebUI Token。
- `napcat_control_dir` / `NAPCAT_CONTROL_DIR`：应用与固定宿主机助手的请求目录。

生产 Compose 还使用 `NAPCAT_ACCOUNT_DIR`。每次账号切换助手将其固定到
`/var/lib/qq_mcp_server/napcat/accounts/<QQ>/qq`，不要手工让两个 NapCat 同时运行。

`[access].allowed_google_emails` 可由 `ALLOWED_GOOGLE_EMAIL` 覆盖。公网模式还需要
`GOOGLE_OAUTH_CLIENT_ID`、`GOOGLE_OAUTH_CLIENT_SECRET`、`MCP_JWT_SIGNING_KEY` 和
`MCP_STORAGE_ENCRYPTION_KEY`。

schema v2/v3 会在首次 v0.6 部署前备份并原子升级到 schema v4，保留群、人物卡、笔记
和消息。Dice Echo 或其他旧 SQLite 不迁移。

## 首次配置群

1. 连接管理 App `/mcp/admin`。
2. 让群内产生一条新消息，或明确调用 `admin.refresh_group_registry`。
3. 调用 `admin.open_group_access`，在短期网页允许 AI 读取目标群。
4. 用 `admin.list_groups` 获取固定 `group_key` 与群 MCP URL。
5. 设置模组、长期 RP 准则，读取成员并绑定玩家/KP/骰娘。
6. 在只属于该团的 ChatGPT 对话连接群 MCP URL。
7. 上传固定模板人物卡并确认。
8. 回到管理 App 启用群。

所有群消息都会经 SSE 入库；访问授权只控制 AI 是否能读，启停只控制跑团工具。

## QQ 账号登记与切换

1. `admin.open_qq_account_registration`：打开网页登记 QQ 号和标签，不输入密码。
2. 确保目标 QQ 已加入全部当前启用的跑团群。
3. `admin.begin_qq_account_switch(target_account_id)`：打开浏览器确认页。
4. 确认后旧 NapCat 停止，服务切到目标账号专属目录并保持采集暂停。
5. `admin.open_napcat_webui`：在 Tailscale 私有面板登录目标 QQ。
6. 登录成功后调用 `admin.complete_qq_account_switch(switch_id)`。

最后一步只执行一次登录信息和一次强制群列表读取。账号或群不匹配时保持目标账号和暂停
状态，不自动切回、不自动重试。失败后同一目标冷却 30 分钟，24 小时最多三次失败。

所有登记账号被视为同一玩家的 QQ 别名，共享群数据。一个时刻仍只有一个采集账号。

## 管理 MCP 工具

群访问与诊断：

- `admin.open_group_access`
- `admin.get_napcat_status`
- `admin.pause_qq_collection` / `admin.resume_qq_collection`
- `admin.refresh_group_registry` / `admin.probe_group`
- `admin.open_napcat_webui` / `admin.open_napcat_recovery`

消息缺口：

- `admin.list_message_gaps`
- `admin.create_message_gap`
- `admin.control_message_gap_repair`
- `admin.accept_message_gap`

QQ 账号：

- `admin.list_qq_accounts`
- `admin.open_qq_account_registration`
- `admin.begin_qq_account_switch`
- `admin.get_qq_account_switch_status`
- `admin.complete_qq_account_switch`
- `admin.cancel_qq_account_switch`

模组：

- `admin.list_groups` / `admin.get_group_setup`
- `admin.list_group_members`
- `admin.update_group_profile`
- `admin.set_member_roles`
- `admin.set_group_enabled`

## 群 MCP 工具

- `trpg.get_status`
- `trpg.open_campaign_dashboard`
- `trpg.get_roleplay_context`
- `trpg.get_character_card`
- `trpg.search_messages`
- `trpg.search_coc_rules`
- `trpg.begin_character_card_upload`
- `trpg.commit_turn_updates`
- `trpg.list_changes`
- `trpg.undo_change`

群工具的群作用域来自 URL。`get_roleplay_context` 在 SSE 未确认健康、采集暂停或返回范围
与未解决缺口重叠时拒绝提供可能误导的上下文。

## AI 路由

Admin 与群 MCP 已内置中文 instructions、工具说明、参数说明、读写注解和下一步，不需要
关键词命令或额外 Skill。Skill 可能方便固定个人工作流，但不会降低服务器响应时间，也
不应绕过浏览器确认、版本检查和账号/缺口安全限制。
