# sigma

一个自研的 coding agent harness。

目标不是功能数量，而是**每一个设计决定都能被追问而不塌**。
所以这个仓库里，约束是被 CI 强制的，指标是有基线的，边界是写明的。

> 当前状态：**P0 骨架完成；P1 计划待审阅，尚未开工。**
> 现阶段可运行的只有三道自检门禁和一个占位 CLI。

## 已拍板的决策

| 编号 | 决策 | 日期 |
| --- | --- | --- |
| a1 | 产品壳范围与草图对齐：**砍掉 Slack Bot / Web UI / RPC Mode** | 2026-09-20 |
| a2 | 所有抽象接口**一律用继承制（`abc.ABC`）**，不用 `typing.Protocol`；数据载体用 Pydantic `BaseModel` | 2026-09-20 |
| a3 | 首个 Provider 做 **OpenAI 兼容协议**（覆盖 DeepSeek / Kimi / GLM / 通义 / Ollama） | 2026-09-20 |

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

> **踩坑提醒**：上面三条命令**必须用项目 `.venv` 里的可执行文件跑**。
> 若 `activate` 没生效（例如在 CI、或在别的 Python 环境里执行），
> `pip install -e` 的包会装到那个环境去，而项目 `.venv` 仍然是空的——
> **症状是 `./.venv/Scripts/lint-imports.exe` 报 `No such file or directory`**。
> 确认方式：`./.venv/Scripts/python.exe -c "import sigma; print(sigma.__file__)"`。
> 详见 `docs/plans/P0-骨架.md` 第 4.5 节。

## 分层

依赖方向严格受限，**由 CI 强制，不靠自觉**。

```
sigma         → sigma_tools, sigma_session, sigma_agent, sigma_ai
sigma_tools   → sigma_agent, sigma_ai
sigma_session → sigma_agent, sigma_ai
sigma_agent   → sigma_ai
sigma_ai      → （无内部依赖）
```

| 层 | 职责 |
| --- | --- |
| `sigma` | 产品壳：CLI / REPL / 一次性模式 / SDK 入口 |
| `sigma_tools` | 内置工具：read / write / edit / bash |
| `sigma_session` | 会话树 · 上下文组装 · 压缩 · 扩展装配 |
| `sigma_agent` | agent loop · 工具注册表 · 钩子总线 · checkpoint |
| `sigma_ai` | Provider 抽象 · 流式事件 · 用量 |

**`sigma_tools` 与 `sigma_session` 是兄弟层，互不依赖。** 两者都只依赖
`sigma_agent` 和 `sigma_ai`。

### 三条契约

写在 `pyproject.toml` 的 `[tool.importlinter]` 里：

| 契约 | 类型 | 作用 |
| --- | --- | --- |
| 分层只允许向下依赖 | `layers` | 禁止下层引用上层 |
| 核心层不得依赖评测与扩展 | `forbidden` | 禁止 core 引用 `evals` / `extensions` |
| 内置工具与会话层互不依赖 | `independence` | 钉住兄弟层 |

为什么兄弟关系要单独一条：`layers` 是线性栈，语义只有「下层不许引用上层」,
**它默认放行所有向下的 import**。只写 `layers` 的话，
`sigma_tools → sigma_session` 会被静默放行。

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
| `docs/architecture.md` | 架构方案 v1.1：六项决策、接口设计、可验证性设计、分阶段计划 |
| `docs/pi-harness研究笔记.md` | Pi Agent Harness 调研，含来源可信度分级 |
| `docs/plans/` | 各阶段的实施计划与验收证据 |
| `docs/decisions/` | 架构决策记录（ADR）。**D1–D6 的「判断依据」与「我接受的代价」两栏待本人填写** |
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

### 不做的东西，以及代替路径

砍功能必须给代替路径，否则"不做"看起来就是"没有"。

| 不做 | 代替路径 |
| --- | --- |
| MCP | 写一个扩展（工具与内置工具走同一条注册路径） |
| 子代理 | 用户在 REPL 里另开一个会话 |
| Plan Mode | 在任务描述里要求先输出计划，人工确认后再让它动手 |
| 待办追踪 | 让 agent 往会话目录写一个 markdown 文件 |
| 后台 bash | 用 `tmux` 或 `&` 自己跑，agent 只负责读输出 |
| Web UI | 用 SDK 自己接（`create_session()` 是公开入口） |
| Slack Bot | 同上，SDK 是唯一需要的接口 |
| RPC / 远程 session | 同上 |
| 多实例编排 | 起两个进程，各自有独立工作区与影子 git |
| SQLite session backend | JSONL 追加写 + 内存索引；检索痛了再说 |
| 15+ Provider | 只做 OpenAI 兼容协议（已覆盖国内主流）+ 按需补 Anthropic |
| 云端 sandbox | 不做，边界写在架构文档里 |

**最后一行是这份列表里唯一一条没有代替路径的**，这是有意的：
它不是"暂时不做"，是这个项目的设计选择。理由见 `docs/decisions/D6-不做容器隔离.md`。
