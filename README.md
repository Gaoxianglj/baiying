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
uv run companion config    # 查看生效配置（密钥打码）
uv run companion init      # 创建记忆文件夹 data/memory/
uv run companion chat --as <你的id>   # 命令行聊天
uv run companion qq        # 连接 NapCat，在 QQ 上运行
uv run companion web       # 本机控制台：状态、设置、种子、人物与记忆、日记
uv run companion sleep     # 手动做一次“睡眠”
uv run pytest
uv run ruff check .
```

## 一键启停（后台运行）

- `start.bat`：依次启动 Ollama → NapCat（用 `.env` 的 `QQ_BOT_ACCOUNT` 快速登录；需要扫码时自动弹出二维码图片，扫完继续）→ 主程序 → 控制台，全部在后台运行，关掉窗口也不会停；最后打开控制台（地址固定，可收藏）
- `stop.bat`：主程序先整理完这次的对话再退出；然后停控制台、NapCat（只停 NapCat 目录里的 QQ，不动你平时用的 QQ）、Ollama。保留某项：`stop.bat -KeepOllama` / `-KeepNapCat`
- `status.bat`：查看各服务是否在运行
- 主程序和控制台崩溃后 5 秒自动重启，且同一时间只允许运行一个实例
- 日志在 `data/logs/`（`qq-日期.log`、`web-日期.log`），运行状态在 `data/run/`
- `.env` 里可设 `NAPCAT_DIR`（NapCat 目录）和 `CONSOLE_PORT`（控制台端口，默认 8765）

## 配置

优先级从低到高：`config.toml`（进仓库）< `config.local.toml`（本机）< `.env`（本机）< 环境变量。API key 只能放 `.env` 或环境变量；复制 `.env.example` 为 `.env` 后填写。
