# qq_mcp_server

`qq_mcp_server` 把当前 NapCat QQ 账号中的多个 COC 跑团群保存到私有 SQLite，并为
ChatGPT 提供彼此隔离的 MCP App。一个群永久对应一个模组、一个当前人物卡和一个群级
MCP 地址；增加新模组时使用新的 QQ 群。可以登记并人工切换多个属于同一玩家的 QQ 号，
账号使用独立 NapCat 登录目录，但共享聊天数据库、人物卡、笔记和模组状态。

QQ 侧始终只读：没有发消息、撤回、禁言、群管理或任意 OneBot 动作透传。MCP 的写操作
只更新本地人物卡、团务笔记与应用配置。

## 已实现的范围

- 正常运行只消费 NapCat 主动推送的反向 WebSocket 事件；不会轮询群列表、成员或历史。
- 独立采集进程每 60 秒读取一次 NapCat 本地在线状态，不产生 QQ 群或历史网络请求。
- 群访问网页只决定 AI/MCP 是否可以读取该群，不影响被动事件采集。
- 事件会话、心跳与 OneBot 动作全部审计；断线、离线和非正常重启会登记消息缺口。
- QQ 恢复在线后先观察五分钟并连续确认两次，再按全局安全额度渐进补偿历史缺口。
- 显式最近消息刷新有十分钟冷却；完整缺口修复仍受全局 60 秒一页、24 小时 30 页限制。
- 离线时 MCP 仍可读取缓存，但会返回 `safe_to_roleplay=false`、警告代码和缺口，禁止伪装成实时。
- 登录失效、账号不匹配、切号或人工暂停会持久化熔断，不会自动重启或反复登录 NapCat。
- 可登记多个 QQ 号并经浏览器二次确认切换；每个账号使用独立 NapCat 登录目录。
- 只读取文字、`@` 和引用关系；纯媒体消息保存为“未读取的媒体消息”，不解析或保存 URL。
- 一个管理 App：维护账号、群访问、消息缺口、模组、成员角色和群级启停。
- 每个已授权群一个不可猜测、不可修改的 `/mcp/groups/{group_key}` 端点，工具不接受群参数。
- 固定浅蓝色 23.1.1 Excel 人物卡，只读取 `人物卡` 工作表；通过 MCP 签发一次性上传、预览、确认链接。
- 动态人物卡和结构化团务笔记可以在一个事务中更新，带版本检查、变更记录和安全定向撤销。
- 群 MCP 可签发一小时有效的只读模组档案页，分页查看 RP 准则、人物卡、笔记、消息和变更。
- 三本私有 COC PDF 离线构建全文索引；运行期只能检索，不能上传 PDF，也没有服务器 LLM。
- Google OAuth 和完整邮箱白名单保护管理 App、群 App 与其签发的短期网页链接。
- Tailscale 私有 NapCat 面板、一次性登录入口，以及浏览器确认的切号/受限恢复流程。
- API 和反向 WebSocket 采集器独立发布；普通 API 更新不会中断采集或重启 NapCat。
- 重新登录稳定五分钟后，白名单且未归档的群会按每分钟一页、每天最多 30 页自动补偿缺口。
- 白名单网页支持归档/恢复；归档群保留历史只读访问和被动入库，不再主动读取 QQ 历史。

不再包含 Dice Echo 解析器、TUI、TXT 导出、旧数据库迁移或关键词命令路由。这是全新数据
库设计，旧人物卡也必须重新上传。

## 本地启动

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)、已登录的 NapCat，以及三本规则书 PDF。

```bash
uv sync
uv run qq_mcp_server setup
export ONEBOT_ACCESS_TOKEN='与 NapCat onebot11.json 一致的 token'

uv run qq_mcp_server build-rules \
  --investigator '/home/kscii/Downloads/克苏鲁的呼唤第七版调查员手册1.16.pdf' \
  --keeper '/home/kscii/Downloads/COC7th核心规则书v1.2.1.pdf' \
  --magic '/home/kscii/Downloads/克苏鲁神话魔法大典 0.1.pdf'

uv run qq_mcp_server run
```

然后连接管理 App `/mcp/admin`，按自然语言要求它“打开群访问授权”，在一次性网页允许 AI 读取群，
再依次设置模组、玩家/KP/骰娘、上传人物卡并启用群。`admin.list_groups` 会返回每个群的
固定 MCP URL 和下一步。

连接群 App 后可以说“打开模组面板”，由 `trpg.open_campaign_dashboard` 返回只绑定
当前群的一小时只读链接。页面不提供编辑能力，所有修改仍通过带版本和审计的 MCP 工具。

人物卡已锁定为同一模板，验证锚点是 `J3=玩家`、`F16=技能名称`、`F79=物品名称`。
`山田何也(1).xlsx` 与 `小林和彦(2).xlsx` 已作为真实解析样本测试；空白卡只有填写人物名
和玩家后才能绑定。

## MCP 地址

- 管理 App：`/mcp/admin`
- 群 App：`/mcp/groups/{group_key}`

建议在 ChatGPT 中只给每个对话连接对应群的 App。群工具里没有 `group_id` 或
`group_key` 参数，所以 AI 不能因参数错误越到另一个群。撤销 AI 访问后，其地址立即
返回 404；归档保留历史只读端点，停用只锁跑团工具，被动事件采集仍继续。

不需要另写 Skill、index 或关键词表。两套 MCP Server 已内置简短 instructions，工具名、
描述、读写注解和返回的 `next_actions` 足够模型从自然语言选择工具。只有在未来出现稳定、
跨 App 的复杂多步流程时，再考虑单独 Skill。

完整接口和配置归属见 [配置与操作](docs/configuration.md)，实现边界见
[架构说明](docs/architecture.md)，生产环境见 [Google Cloud 部署](docs/deployment.md)。

## 安全与许可

使用非官方 QQ 接入可能触发登录验证或账号风险。请只同步已取得成员同意的群，不要公开
NapCat WebUI、OneBot 端口、规则书 PDF、人物卡链接或无鉴权 MCP。更多边界见
[SECURITY.md](SECURITY.md)。项目使用 [MIT License](LICENSE)。
