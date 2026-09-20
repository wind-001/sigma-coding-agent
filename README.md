# sigma

一个自研的 coding agent harness。

目标不是功能数量，而是**每一个设计决定都能被追问而不塌**。
所以这个仓库里，约束是被 CI 强制的，指标是有基线的，边界是写明的。

> 当前状态：**P0 骨架完成，尚未有功能实现。**
> 现阶段可运行的只有三道自检门禁和一个占位 CLI。

---

## 快速验证

```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows
# source .venv/bin/activate        # macOS / Linux
pip install -e ".[dev]"

lint-imports    # 分层依赖契约
mypy core       # 类型检查
pytest -q       # 单测，不需要 API key
```

三道全过 = P0 达标。

## 分层

依赖方向严格单向，**由 CI 强制，不靠自觉**。

```
sigma           产品壳：CLI / REPL / 一次性模式 / SDK 入口
  ↓
sigma_tools     内置工具：read / write / edit / bash
  ↓
sigma_session   会话树 · 上下文组装 · 压缩 · 扩展装配
  ↓
sigma_agent     agent loop · 工具注册表 · 钩子总线 · checkpoint
  ↓
sigma_ai        Provider 抽象 · 流式事件 · 用量
```

五层单向依赖，外加一条「核心层不得依赖 `evals/` 与 `extensions/`」。
两条契约都写在 `pyproject.toml` 的 `[tool.importlinter]` 里。

## 目录

```
core/           五个包（package-dir 指向 core/，所以它们是顶层包）
tests/          单测。fixtures/arch/ 里是契约测试的正反两个样例
evals/          评测：数据集与运行器（P1 起填充）
extensions/     运行时加载的扩展样例
docs/           架构方案、调研笔记、计划、决策记录
.github/        CI
```

## 文档

| 文件 | 内容 |
| --- | --- |
| `docs/architecture.md` | 架构方案 v1.0：六项决策、接口设计、可验证性设计、分阶段计划 |
| `docs/pi-harness研究笔记.md` | Pi Agent Harness 调研，含来源可信度分级 |
| `docs/plans/` | 各阶段的实施计划与验收证据 |
| `docs/decisions/` | 架构决策记录（ADR） |
| `AGENTS.md` | 本项目的开发约定 |

## 为什么有这些约束

三条最容易违反的约定，全部做成了机器可查：

1. **分层不能反向** —— 反向 import 由 `lint-imports` 拦。
2. **常驻上下文不能变** —— 会话内常驻区一旦变动，prompt cache 从变动点起全部失效。
3. **扩展热重载要找得到新对象** —— 任何调用方都不得缓存工具实例。

第 2、3 条在 P2 / P4 落地时补进测试。

## 已知取舍

- **不做容器隔离。** 宿主环境不受保护，安全上只有钩子软边界 + 工作区根约束 + git checkpoint 三层。边界写清在 `docs/architecture.md` 第 6 节。
- **不做 TUI。** 用纯文本 REPL，避免拖累 CI。
- **不做 MCP / 子代理 / Plan Mode。** 这些留给扩展。
