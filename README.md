# sigma

一个自研的 coding agent harness。

目标不是功能数量，而是**每一个设计决定都能被追问而不塌**。
所以这个仓库里，约束是被 CI 强制的，指标是有基线的，边界是写明的。

> 当前状态（截至提交 `93a4b9d`）：**P1 批次 0 / 1 / 1.5 已完成；
> 批次 2–4 的 agent loop 已跑通**（`sigma -p "任务"` 可用）。
> 当前盘上 `176 个单测全绿`、三道门禁全绿、注入实验 **17/17** 证伪成功
> （批次 1 的 7 条 + 批次 1.5 的 10 条）。
>
> **真实 API 已跑通两层**：Provider 层冒烟 5/5（`scripts/real_api_smoke.py`）、
> **agent loop 端到端**（`scripts/real_api_agent_demo.py` 与 CLI 实测均通过）。
> 未验证项见 `docs/plans/P1-批次1-详规.md` 第 9 节（§9.1 复核结果、
> §9.2 一处测试设计缺陷）与 `P1-批次1.5-详规.md` 第 9 节。
>
> **尚未落地**：`edit` / `bash` / `grep` 三个工具、批次 2–4 的门槛注入实验
> （**G22–G31 目前只写在文档里，按本项目纪律不算"生效"**）、
> `tests/fixtures/transcripts/` 六个回放场景、评测运行器（批次 5）。
>
> **⚠️ 计数纪律（2026-09-20 评审新增）**：本文件里任何计数（测试数 / 文件数 /
> 契约数）都必须绑定提交号。理由：批次 1.5 的代码曾未提交就落在盘上，
> 导致"104 个单测"这句在提交历史里已过期而无人察觉。
> 这是「配置跑绿」的第三种形态——**数字会随未提交代码漂移**。

## 已拍板的决策

| 编号 | 决策 | 日期 |
| --- | --- | --- |
| a1 | 产品壳范围与草图对齐：**砍掉 Slack Bot / Web UI / RPC Mode** | 2026-09-20 |
| a2 | 所有抽象接口**一律用继承制（`abc.ABC`）**，不用 `typing.Protocol`；数据载体用 Pydantic `BaseModel` | 2026-09-20 |
| a3 | 首个 Provider 做 **OpenAI 兼容协议**（覆盖 DeepSeek / Kimi / GLM / 通义 / Ollama） | 2026-09-20 |
| Q0 | P1 评测仓库用**自造迷你仓库** | 2026-09-20 |
| Q1 | 内置工具 **5 个**：`read` / `write` / `edit` / `bash` / `grep` | 2026-09-20 |
| Q2 | `truncate.py` 截断部分**提前到 P1**（不分页、不落盘） | 2026-09-20 |
| Q3 | 首个被测模型 **DeepSeek `deepseek-chat`** | 2026-09-20 |
| Q4 | `-p` 退出码：**0 = 正常结束，非 0 = harness 自身失败** | 2026-09-20 |
| R2 | 时间预算：**8h/周全部给 sigma**（与 interview-agent 无并行关系） | 2026-09-20 |

---

## 跑一个真实任务

密钥只需配置一次（见下方「密钥放哪儿」），之后直接跑：

```bash
sigma -p "读取 input.txt 里的数字，加 5 后写入 result.txt" --workspace ./demo
```

想先建一个能马上试的工作区：

```bash
python scripts/make_demo_workspace.py    # 在项目下建 demo/，含示例文件与可试任务
```

输出会逐条列出模型说了什么、调了哪个工具、工具返回了什么：

```
[1] 模型: 说 "I'll start by reading the input file."；调用 read({'path': 'input.txt'})
[2] 结果 <- read: '1\t7'
[3] 模型: 说 'The input is `7`. Adding 5 gives 12.'；调用 write({'path': 'result.txt', 'content': '12'})
[4] 结果 <- write: '新建 ...result.txt，现在 2 字节'
[5] 模型: 说 '读取 input.txt 得到数字 7，加 5 后为 12，已写入 result.txt。'
```

| 参数 | 说明 |
| --- | --- |
| `-p, --prompt` | 任务描述。不传则只打印帮助 |
| `--workspace` | 工作区根目录（相对路径的基准）。**不是安全边界** |
| `--preset` | `deepseek`（默认）/ `moonshot` / `zhipu` / `dashscope` / `ollama` |
| `--model` / `--base-url` / `--api-key` | 覆盖预设；key 也可走 `SIGMA_API_KEY` |
| `--max-rounds` | 轮数上限，默认 20 |
| `--temperature` | 采样温度，默认 0 |

## 密钥放哪儿

**优先级（高 → 低）**，第一个命中的胜出：

| 顺序 | 位置 | 适合 |
| --- | --- | --- |
| 1 | `--api-key sk-xxx` | 只用一次 |
| 2 | 环境变量 `SIGMA_API_KEY` | 临时换一个 |
| 3 | `~/.sigma/.env` | **推荐**：在 git 仓库之外，不可能被误提交 |
| 4 | `./.env` | 方便，但安全性依赖 `.gitignore` 挡着 |

推荐第 3 种。用编辑器新建 `~/.sigma/.env`（Windows 上是
`C:\Users\<你的用户名>\.sigma\.env`），内容一行即可：

```
SIGMA_API_KEY=sk-你的key
```

配好之后 CLI 会把来源打出来——**这条是为了排查"改了却没生效"**：

```
  密钥    已加载（来源：配置文件 C:\Users\...\.sigma\.env）
```

`.env` 的解析交给 **python-dotenv**，所以它支持的写法我们全支持
（引号、转义、`export` 前缀、`${VAR}` 变量展开）。

> 2026-09-20 更正：这里最初是自己写的 20 行解析器，理由是"能不新增依赖就不新增"。
> **那是把原则用错了地方**——该原则的适用条件是"新增依赖会带来实质代价"
> （当时拒 OpenAI SDK 是因为它藏起了要验证的协议细节），而 python-dotenv
> 是纯 Python、零传递依赖。代价核算下来：自写版本 20 行 + **18 个测试**，
> 覆盖面仍不如成熟库。改成依赖后测试降到 9 个——**省下的正是"验证自己造轮子"的成本。**

**退出码**（Q4 已拍板）：`0` = 正常结束（**任务成没成都不影响**）；
非 `0` = harness 自身失败（缺 key / 工作区不存在 / 连不上 provider）。

> ⚠️ **P1 的工具没有任何边界约束。** D5 的三层软边界（工作区约束 /
> 影子 checkpoint / 钩子规则）**一层都没落地**，`read` 可读任意路径、
> `write` 可写任意路径。**只在受控目录内使用。**
> 不知道边界在哪比边界不存在更危险——所以 CLI 每次启动都会打印这条。

> 📌 **当前工具集只有 `read` / `write`**——`edit` / `bash` / `grep` 尚未实现。
> 所以模型改文件只能整文件重写，那更费 token、也更容易出错。
> **这一点写出来，不假装够用。**

> 🧪 **不开 REPL**：交互模式需要 steering / follow-up 双队列（属 P3），
> 现在做只能做一个"读了输入但没人处理"的假货。

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

三道全过 = 当前阶段达标。

**门槛注入实验**（验证"约束真的在生效"，而不只是"配置跑绿"）：

```bash
python scripts/gate_injection_batch1.py
```

它逐条对 G1–G4 / G6 / G9 / G10 注入破坏 → 确认对应断言变红 → 还原。
输出 `7/7 条门槛被成功证伪` 才算通过。

```bash
python scripts/gate_injection_batch15.py    # 输出 `10/10 条门槛被成功证伪`
```

**真实 API 冒烟**（需要 API key，且**只测 `sigma_ai` 层**，不需要 agent loop）：

```bash
export SIGMA_API_KEY=sk-...
python scripts/real_api_smoke.py --preset deepseek
```

五项探测对应批次 1 详规第 9 节的 R2 / R4 / R5 / R6。
支持 `--preset deepseek|moonshot|zhipu|dashscope|ollama`，也可用
`--base-url` / `--model` 直接指定。**key 只从环境变量或命令行读，不落盘、不打印。**

> **注意**：这个脚本会**临时修改真实仓库的源文件**，还原放在 `finally` 里。
> 不要在它有未提交改动时并行做别的事。
> 为什么不能"复制一份到临时目录再改"——见 `docs/plans/P1-批次1-详规.md` 第 10.4 节坑一。

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
| `sigma_tools` | 内置工具：read / write / edit / bash / grep |
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
| `docs/architecture.md` | 架构方案 v1.3：六项决策、消息模型两层结构、接口设计、可验证性设计、分阶段计划 |
| `docs/pi-harness研究笔记.md` | Pi Agent Harness 调研，含来源可信度分级 |
| `docs/plans/` | 各阶段的实施计划与验收证据 |
| `docs/decisions/` | 架构决策记录（ADR）。**D1–D6 的「判断依据」与「我接受的代价」两栏待本人填写**；集中填写清单见 `docs/decisions/_待填清单.md` |
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
