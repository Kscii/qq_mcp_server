# 安全策略

## 安全边界

`qq_mcp_server` 对 QQ 永久只读。OneBot 客户端只允许以下动作：

- `get_login_info`
- `get_group_info`
- `get_group_list`
- `get_group_member_list`
- `get_group_msg_history`

项目不提供发送消息、撤回、禁言、QQ群管理、任意 OneBot 动作透传或任意 SQL 工具。
MCP 可以在本地 SQLite 中更新人物卡、团务笔记和应用配置；这些写入不触碰 QQ。
NapCat 和 OneBot 只能监听服务器回环地址。公网只应暴露 Caddy 的 HTTPS 端口。

远程 MCP 必须启用 Google OAuth，并用完整邮箱白名单做第二次授权。群聊原文是不可信
输入；MCP 返回会明确要求客户端不要执行群消息中的指令。每个群使用不可猜测且不可
修改的 `group_key` 独立端点，白名单移除后端点立即失效。

白名单页和人物卡上传页只通过已登录管理/群 MCP 签发的短期、一次性 bearer 链接访问。
链接可能授权一次操作，不能转发或写入日志。人物卡只接受已锁定模板、只读取 `人物卡`
工作表，文件限制 16 MiB。规则 PDF 仅在部署者离线构建索引时读取，运行期不开放上传。

## 报告问题

请不要在公开 issue 中粘贴 QQ 登录信息、群消息、OAuth 密钥或 OneBot token。
涉及敏感信息时，请通过 GitHub 的私密安全报告功能联系维护者。
