# 参与开发

```bash
uv sync --all-groups
uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest
```

提交信息使用简短的 Conventional Commits 风格，例如：

```text
feat: 增加新的群级只读查询工具
fix: 修复中断后的分页恢复
docs: 补充服务器部署说明
```

任何新增 OneBot 动作都必须先证明是读取动作，并补充安全测试。发送和群管理功能不在
本项目范围内。
