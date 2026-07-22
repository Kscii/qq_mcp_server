# qq_mcp_server

`qq_mcp_server` 是一个小型、永久只读的 QQ 群文字归档和 MCP 服务。它通过 NapCat
的 OneBot 11 HTTP 接口读取一个明确配置的群，将消息保存到 SQLite，同时生成一份
包含发送人、QQ 号和时间的 UTF-8 纯文本文件。

QQ 发送、撤回、群管理和任意 OneBot 动作透传不会被实现。

## 功能

- 首次导入 NapCat 可获取的全部历史，分页中断后可以续传。
- 默认每 15 秒执行重叠增量同步，依靠消息 ID 幂等去重。
- 只保留文字、`@` 和引用消息 ID，丢弃图片、表情、语音、视频和文件。
- SQLite 是唯一权威数据；每个部署目标群生成一个纯文本文件。
- 提供最近消息、条件搜索和同步状态三个只读 MCP 工具。
- 公网模式使用 HTTPS、Google OAuth 和完整邮箱白名单。
- GitHub Actions 用 OIDC 向 Google Cloud 发布，不保存云服务账号密钥。

## 本地使用

需要 Python 3.12 以上、[uv](https://docs.astral.sh/uv/) 和已经配置好的 NapCat。

```bash
uv sync
uv run qq_mcp_server setup
export ONEBOT_ACCESS_TOKEN='与 NapCat onebot11.json 一致的 token'
uv run qq_mcp_server sync
uv run qq_mcp_server run
```

其他命令：

```bash
uv run qq_mcp_server status
uv run qq_mcp_server export
```

详细配置见 [配置与命令](docs/configuration.md)。

## MCP

服务端点为 `/mcp`，只暴露：

- `get_recent_messages`
- `search_messages`
- `get_sync_status`

在没有 `PUBLIC_URL` 时服务仍可用于本机测试，但只应监听 `127.0.0.1`。配置
`PUBLIC_URL` 后，程序会强制要求 Google OAuth 客户端、稳定签名/加密密钥和邮箱
白名单。

## 服务器部署

推荐配置是 2 vCPU、4 GB 内存、30 GB 磁盘和 2 GB swap。生产部署由三个容器组成：
NapCat、应用和可选的 Caddy。没有 PostgreSQL、Redis、消息队列或 Kubernetes。

参见 [Google Cloud 部署](docs/deployment.md) 和 [架构说明](docs/architecture.md)。

## 数据与撤回

纯文本格式示例：

```text
[2026-07-22 19:03:21 +0800] 佐藤健一（QQ 2408924009）
#佐藤扶着墙壁站稳。

"腹痛还没有缓解。"
```

第一版不监听撤回事件；已经归档的文字会保留。TXT 只是 SQLite 的可读导出，不应
手工编辑后再作为程序输入。

## 安全与许可

使用非官方 QQ 接入可能触发登录验证或账号风险。请仅归档已取得成员同意的群，并且
不要把 NapCat WebUI、OneBot 或无鉴权 MCP 暴露到公网。更多边界见
[SECURITY.md](SECURITY.md)。

项目使用 [MIT License](LICENSE)。
