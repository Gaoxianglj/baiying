# 白璎：长期记忆 AI 伙伴

一个记忆存在本地的 AI 伙伴。核心是长期记忆；QQ 聊天和《我的世界》是两个验收用的“身体”。模型可以随时更换，记忆和人格不会丢。

私有仓库。公开后不允许商用。

## 文档

- [可行性与方向报告](docs/feasibility-report.md)
- [项目执行计划](docs/project-plan.md)：任务、进度、决策记录

## 目录

```
src/companion/
  core/      记忆核心编排：事件入口、上下文组装、睡眠整理
  memory/    存储层：事件日志、SQLite 索引、自我与人物档案
  models/    模型网关：GPT 订阅、兜底 API、本地模型
  bodies/    身体适配器：QQ、我的世界
tests/       测试
docs/        报告与计划
data/        运行时记忆数据（不进仓库）
```

## 开发

需要 [uv](https://docs.astral.sh/uv/) 和 Python 3.13。

```bash
uv sync
uv run companion --version
uv run pytest
uv run ruff check .
```

本机配置：复制 `.env.example` 为 `.env` 后填写。`.env` 不进仓库。
