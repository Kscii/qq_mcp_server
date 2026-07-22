# 配置与命令

## 命令

```bash
qq_mcp_server setup
qq_mcp_server sync
qq_mcp_server run
qq_mcp_server status
qq_mcp_server export
```

`setup` 只创建最小 TOML，不启动 QQ。`sync` 在数据库尚未完成初次导入时，会导入
NapCat 可获取的全部历史；中断后再次运行会续传。完成后，`sync` 只读取最新重叠历史。

## 目标群

一个部署实例只允许一个 `qq.group_id`。修改群号时，应同时改用新的数据库和 TXT 路径，
避免把两个群放进同一文件。服务器可以用环境变量覆盖：

- `QQ_ACCOUNT_ID`
- `QQ_GROUP_ID`
- `QQ_GROUP_NAME`
- `DATABASE_PATH`
- `EXPORT_PATH`

## 纯文本格式

```text
[2026-07-22 19:03:21 +0800] 角色名（QQ 123456789）
#角色扶着墙站稳。

"腹痛还没有缓解。"
```

图片、表情、语音、视频、文件和富媒体不会写入数据库。混合消息只保存文字和 `@`，
引用只保存目标消息 ID。

## MCP 工具

- `get_recent_messages(limit, before_message_id)`
- `search_messages(query, sender_qq, start_time, end_time, limit)`
- `get_sync_status()`

所有工具均声明 `readOnlyHint`，并且固定查询配置中的目标群。
