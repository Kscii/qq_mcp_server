# 架构

```text
NapCat / QQ
    │ 仅回环 OneBot HTTP
    ▼
qq_mcp_server
    ├── SQLite：唯一权威数据
    ├── UTF-8 TXT：每个部署目标群一个文件
    └── FastMCP：三个只读查询工具
             │
             ▼
       Caddy / HTTPS / Google OAuth
```

## 模块边界

- `onebot.py`：只有三个只读动作的 HTTP 客户端。
- `normalization.py`：提取文字、发送人、引用 ID，丢弃媒体数据和 URL。
- `store.py`：SQLite 事务、去重、分页和搜索。
- `sync.py`：全量向前翻页、断点续传、15 秒增量重叠同步。
- `exporter.py`：从 SQLite 原子重建人类可读 TXT。
- `mcp_server.py`：最近消息、搜索、同步状态和 OAuth 邮箱白名单。
- `cli.py`：初始化、单次同步、持续运行、状态和导出。

程序只有一个 Python 服务进程。没有 PostgreSQL、Redis、消息队列、WebSocket 或
后台任务框架。SQLite 每次操作使用短连接和 WAL，因此同步循环与 MCP 查询可以安全
共享同一数据库文件。

## 数据一致性

消息唯一键是 `(group_id, message_id)`。NapCat 历史接口的分页游标可能包含上一页
最后一条消息，因此每一页都允许重复并依赖 SQLite 去重。全量导入每页保存最旧游标；
进程中断后从该游标继续。增量同步向历史方向翻页，直到发现数据库中最新的边界消息。

TXT 不是输入数据源。程序只在 SQLite 成功提交新消息后，从数据库写临时文件并原子
替换正式文件。撤回事件不处理，已经归档的文字会保留。
