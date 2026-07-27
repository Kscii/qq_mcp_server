# 配置与操作

## 配置应该放在哪里

| 内容 | 唯一入口 | 原因 |
|---|---|---|
| QQ 账号、OneBot 私有地址、数据库/卡片/规则路径、OAuth、公网地址、同步参数 | TOML/环境变量，部署时很少改 | 服务基础设施和秘密不能交给群聊决定 |
| QQ 群加入/移出采集白名单 | 管理 MCP 签发的一次性网页 | 这是唯一需要人工确认群列表的配置 UI |
| QQ 登录与 NapCat 恢复 | 管理 MCP 签发的一次性确认页 | 长期 Token 不进入 AI；重启还需要人工确认 |
| 模组名、显示名、本群长期 RP 准则 | `admin.update_group_profile` | AI 可先读版本并可靠地结构化更新；准则最多 4096 字 |
| 玩家、KP、骰娘 QQ 号 | `admin.list_group_members` 后调用 `admin.set_member_roles` | 绑定稳定 QQ 号，不绑定易变昵称 |
| 跑团启用/停用 | `admin.set_group_enabled` | 只允许白名单群；停用不停止消息同步 |
| 当前 Excel 人物卡 | 群 MCP 签发的一次性上传/预览/确认页 | MCP 发起且绑定当前群，Web 只承担文件选择器 |
| HP/SAN/MP、技能、物品等动态值 | `trpg.commit_turn_updates` | 原子更新、来源消息、版本和撤销均可审计 |
| 重要线索、人物、地点、目标等团务笔记 | 用户确认后 `trpg.commit_turn_updates` | 避免 AI 把模糊叙事写成事实 |
| 查看当前群已保存的完整模组资料 | `trpg.open_campaign_dashboard` 签发的一小时只读页 | 固定绑定当前群，不增加 Web 编辑入口 |
| 三本规则书 | 部署者执行 `build-rules` | PDF 私有、固定且不应出现在运行期 WebUI |

WebUI 没有通用设置页或人物卡编辑器。它只呈现白名单按钮、一次性的文件上传确认，以及
群 MCP 签发的只读模组档案。正常跑团和全部资料修改仍在 MCP 对话中完成。

## 命令

```bash
qq_mcp_server setup
qq_mcp_server build-rules --investigator INVESTIGATOR.pdf --keeper KEEPER.pdf --magic MAGIC.pdf
qq_mcp_server sync
qq_mcp_server run
qq_mcp_server status [--json]
qq_mcp_server backup
qq_mcp_server pause-collection --reason "维护"
qq_mcp_server prepare-napcat DIRECTORY
```

`setup` 只创建基础 TOML。`build-rules` 原子重建私有规则索引。`sync` 对当前所有白名单
群执行一次最近同步和已配置日期的历史回填；持续运行时不必另开 `sync`。`status`、
`backup` 和 `pause-collection` 都不连接 QQ，适合健康检查、升级备份和安全维护。
`prepare-napcat` 生成仅监听回环、只开 HTTP API 的 NapCat 配置。

## TOML 与环境变量

示例见 [`config.example.toml`](../config.example.toml)。群号、群名和模组不属于静态配置。

`[qq]`：

- `account_id`：NapCat 登录 QQ；可用 `QQ_ACCOUNT_ID` 覆盖。
- `onebot_url`：允许回环 HTTP，或仅 Tailnet 可达的 `.ts.net` HTTPS；可用 `ONEBOT_URL` 覆盖。
- `onebot_sse_url`：同上且路径必须为 `/_events`；默认 `127.0.0.1:3001`。
- `poll_interval_seconds`：全局最近消息对账周期，默认 60 秒。
- `registry_refresh_seconds`：白名单任务刷新周期，1–60 秒。
- `group_discovery_interval_seconds`：主动强制刷新群列表的周期，默认 900 秒。
- `context_freshness_seconds`：跑团上下文允许的最大成功对账年龄，默认 180 秒。
- `sync_concurrency`：兼容旧配置；运行期固定使用全局单并发。
- `page_size`：历史分页大小，1–500。
- `backfill_min_delay_seconds` / `backfill_max_delay_seconds`：旧历史页间随机等待，默认 2–5 秒。
- `backfill_pages_per_cycle`：每轮全局最多回填页数，默认 3。
- `unreachable_backoff_max_seconds`：网络不可达退避上限，默认 900 秒。
- `initial_collection_paused` / `INITIAL_COLLECTION_PAUSED`：首次创建安全状态时是否暂停。
- `request_timeout_seconds` / `history_timeout_seconds`：普通/历史接口超时。
- `history_since`：仅兼容旧部署的全局下限；新部署通过 `admin.update_group_profile`
  为每群设置，未设置时只保留最近一页并等待新消息。

`[storage]`：

- `database` / `DATABASE_PATH`：全新 v0.2 数据库。
- `cards` / `CARD_STORAGE_DIR`：当前人物卡与上传暂存目录。
- `rules` / `RULES_DATABASE_PATH`：离线构建的三书索引。
- `timezone`：MCP 展示消息时间使用的 IANA 时区。
- `oauth` / `OAUTH_STORAGE_DIR`：Google OAuth 持久化状态的根目录；服务按不兼容的
  加密格式在其中使用版本化子目录，无法解密的缓存按未命中处理，升级或密钥轮换时
  不会因旧密文阻断授权。

`[server]`：

- `host`、`port` / `PORT`：监听地址和端口。
- `public_url` / `PUBLIC_URL`：公网 HTTPS 根地址；设置后强制启用 OAuth 和邮箱白名单。
- `upload_token_ttl_seconds`：一次性网页链接时效，60–3600 秒，默认 600。
- `napcat_webui_url` / `NAPCAT_WEBUI_URL`：严格限制为
  `https://<设备>.ts.net:8443/webui` 的 Tailscale 私有地址。
- `napcat_webui_config` / `NAPCAT_WEBUI_CONFIG_PATH`：只在跳转确认时读取当前
  `webui.json` Token。
- `napcat_control_dir` / `NAPCAT_CONTROL_DIR`：应用与主机恢复助手之间的固定请求目录。

`[access].allowed_google_emails` 可用单个 `ALLOWED_GOOGLE_EMAIL` 覆盖。公网模式还必须设置
`GOOGLE_OAUTH_CLIENT_ID`、`GOOGLE_OAUTH_CLIENT_SECRET`、`MCP_JWT_SIGNING_KEY` 和
`MCP_STORAGE_ENCRYPTION_KEY`。

本项目 v0.4 的 schema v2 会在首次 v0.5 启动前备份并原子升级到 v3，保留白名单、人物卡
和消息。旧版 qq_mcp_server 或 Dice Echo 数据仍不迁移，也不要把它们的 SQLite 改名后
继续使用。

## 第一次配置一个群

1. 在 ChatGPT 连接管理 App `/mcp/admin`。
2. 说“打开群白名单”，AI 调用 `admin.open_group_whitelist`；打开短期网页加入群。
3. 调用 `admin.list_groups`，选定它返回的 `group_key` 与固定群 App URL。
4. 设置永久模组名；调用成员列表并绑定一个玩家，以及可选的 KP/骰娘。
5. 在 ChatGPT 新建/选择只属于这个团的对话，连接该群 URL。
6. 说“上传人物卡”，通过返回链接选择固定模板 XLSX，检查预览并确认。
7. 回到管理 App 启用群。规则、模组、玩家、卡片准备好之前，启用会返回缺项而不部分成功。

白名单是“允许采集”；启用是“允许跑团工具”。白名单内停用群仍同步。移出白名单才会
停止同步并撤销群端点。一个群不切换模组，新团建立新群。

## 管理 MCP 工具

- `admin.open_group_whitelist()`：签发一次性白名单页。
- `admin.get_napcat_status()`：诊断登录、OneBot、SSE、群注册表和逐群同步新鲜度。
- `admin.pause_qq_collection(reason)`：持久化暂停全部 QQ 读取。
- `admin.resume_qq_collection()`：用户重新登录后单次校验账号并恢复。
- `admin.refresh_group_registry()`：带 60 秒冷却的强制 `no_cache` 群列表刷新。
- `admin.probe_group(group_id)`：列表缺失时直接验证当前 QQ 是否能访问指定群。
- `admin.open_napcat_webui()`：签发不向 AI 暴露长期 Token 的私有面板入口。
- `admin.open_napcat_recovery()`：取得用户明确同意后签发重启二次确认页。
- `admin.list_groups()`：群、固定 URL、版本、同步和下一步。
- `admin.get_group_setup(group_key)`：单群完整准备清单。
- `admin.list_group_members(group_key, query?, limit)`：读取稳定 QQ 号。
- `admin.update_group_profile(group_key, expected_version, ...)`：模组、标签、每群回填起点和
  最多 4096 字的长期 RP 准则。
- `admin.set_member_roles(group_key, expected_version, player, kp?, dice_bot?)`：成员角色。
- `admin.set_group_enabled(group_key, expected_version, enabled)`：群级启停。

管理写工具只应响应 ChatGPT 用户直接要求，不能因为 QQ 群正文触发。所有写工具使用
`expected_version`；冲突后先重新读，不自动覆盖。

## 群 MCP 工具

- `trpg.get_status()`：即使停用也可用的准备与诊断。
- `trpg.open_campaign_dashboard()`：签发绑定当前群、一小时有效且可重复浏览的只读档案页。
- `trpg.get_roleplay_context(since_message_id?, before_message_id?, limit)`：正常拟定回复的
  单次聚合读取；两个游标互斥，默认 30、最多 100 条，并返回下一页游标。
- `trpg.get_character_card(view="roleplay"|"full")`：当前卡和可选单元格来源。
- `trpg.search_messages(query?, sender_qq_user_id?, after?, before?, limit)`：旧消息条件搜索。
- `trpg.search_coc_rules(query, book="all"|"investigator"|"keeper"|"magic", limit)`：规则检索。
- `trpg.begin_character_card_upload()`：签发绑定本群的 XLSX 上传确认页。
- `trpg.commit_turn_updates(expected_version, origin, summary, card_operations?, note_operations?)`：原子更新。
- `trpg.list_changes(limit, before_change_id?)`：审计 AI 写入。
- `trpg.undo_change(change_id, expected_version, reason)`：定向撤销整次写入。

所有群工具的群作用域来自 URL，刻意不接受群名、群号或 `group_key`。人物卡操作采用 JSON
Pointer 的 `set`、`increment`、`add`、`remove`；笔记类别是 `clue`、`npc`、`location`、
`objective`、`event`、`other`。自动机械更新必须附本群
真实消息 ID；重要线索先征得用户同意。

## AI 行为，不使用关键词路由

Admin 和群 MCP 已带内置 instructions。模型应从用户自然语言和工具描述自行选择接口，
不需要输入 `/命令`，也不需要附加 Skill/index：

- 正常拟定跑团回复：一次 `trpg.get_roleplay_context`，默认给三个动态命名且明显不同的候选。
- 精确规则确实影响回答/检定建议时：再用 `trpg.search_coc_rules`。
- 明确 HP/SAN/MP、技能、物品等结果：可以自动提交并回报 `change_id`。
- 多条重要未记录线索：先列标题询问，用户同意后提交笔记。
- 用户说“撤销刚才更新”：先读变更，按明确 `change_id` 整批撤销。

符号约定是语义提示，不是服务器解析命令：`#` 是当前人物动作，双引号是人物对白，圆括号
是玩家场外发言；只有绑定玩家 QQ 的圆括号才代表当前人物玩家。无符号文本由 AI 结合角色
和上下文判断，低置信推断不得持久化。
