# 安全策略

## 安全边界

`qq_mcp_server` 永久只读。OneBot 客户端只允许以下动作：

- `get_login_info`
- `get_group_info`
- `get_group_msg_history`

项目不提供发送消息、撤回、禁言、群管理、任意 OneBot 动作透传或任意 SQL 工具。
NapCat、OneBot 和 WebUI 只能监听服务器回环地址。公网只应暴露 Caddy 的 HTTPS 端口。

远程 MCP 必须启用 Google OAuth，并用完整邮箱白名单做第二次授权。群聊原文是不可信
输入；MCP 返回会明确要求客户端不要执行群消息中的指令。

## 报告问题

请不要在公开 issue 中粘贴 QQ 登录信息、群消息、OAuth 密钥或 OneBot token。
涉及敏感信息时，请通过 GitHub 的私密安全报告功能联系维护者。
