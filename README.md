# sigma

一个自研的 coding agent harness。

目标不是功能数量，而是**每一个设计决定都能被追问而不塌**。
所以这个仓库里，约束是被 CI 强制的，指标是有基线的，边界是写明的。

> 当前状态（截至本次提交，README 与代码同一提交）：**P1 批次 0 / 1 / 1.5 已完成；
> 批次 2–4 的 agent loop 已跑通**（`sigma -p "任务"` 可用），**G22–G31 十条门槛
> 已全部做到能被单独证伪**（其中五条此前连测试都没有，已补齐）；
> 批次 6 完成**流式渲染 + 交互模式 + 启动入口**（G32–G36 五条新门槛）；
> 批次 7 完成 **web_search 联网搜索**（Tavily，G37–G41）；
> 批次 8 完成**调研纪律 + web_fetch 精读**（Firecrawl，G42–G47）；
> P2-1 完成**回放场景 + 最小评测运行器**（G55–G56、G58，见 `evals/`）；
> P2-2 完成**会话树与存储**（`sigma_session/tree.py` + `store.py`，G48/G49/G53）；
> P2-3 完成**上下文接树 + `AGENTS.md` 注入**（`resources.py`，常驻区预算 G59）；
> P2-4 完成**上下文压缩**（`compact.py`：视图式，不改历史；G51/G52/G60）；
> P2-5 完成 **CLI 会话接续**（`--continue` / `--session`；G61/G62/G63）。
> 当前盘上 `424 个单测全绿`、三道门禁全绿（mypy strict 49 files / 契约 3 kept）、
> 注入实验 **64/64** 证伪成功
> （批次 1 的 7 条 + 批次 1.5 的 10 条 + 批次 2–4 的 10 条 + 批次 6 的 6 条
> + 批次 7 的 5 条 + 批次 8 的 7 条 + P2-1 的 4 条 + P2-2 的 3 条 + P2-3 的 4 条 + P2-4 的 4 条
> + P2-5 的 4 条）。
> **注意**：这些数字只对应当前工作区状态；换代码或换环境后**必须重跑**——
> 注入锚点与源码硬耦合，数字会随未提交代码漂移（详见下方「门槛注入实验」）。
> 另注：P2-1 曾试图加一条「回放工作区隔离」门槛（G57），
> **实测证伪不了，故未注册**——宁可少一条门槛，不要一条名义门槛。
>
> **真实 API 已跑通两层**：Provider 层冒烟 5/5（`scripts/real_api_smoke.py`）、
> **agent loop 端到端**（`scripts/real_api_agent_demo.py` 与 CLI 实测均通过）；
> 五工具就位后另做了一次 CLI 真实冒烟：模型用 `grep` 定位 → `read` 确认 →
> `edit` 一次改对（2026-09-20）。
> 未验证项见 `docs/plans/P1-批次1-详规.md` 第 9 节（§9.1 复核结果、
> §9.2 一处测试设计缺陷）与 `P1-批次1.5-详规.md` 第 9 节。
>
> **尚未落地**：`evals/datasets/` 的 70 条任务集（需真实 API key + 判定脚本）。
> 回放场景与最小运行器**已落地**（`tests/fixtures/transcripts/` 六个 + `evals/runner.py`）。
>
> ⚠️ 因此 **P2 的第 3 条验收门（压缩前后成功率下降 ≤ 5%）未验收**——
> 它需要有真模型来判定"压完还够不够用"，离线用回放跑出来的"成功率"
> 只是在数脚本对不对得上，与压缩质量无关。**未验收 ≠ 已通过。**
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
| Q5 | 联网搜索做成**可选第 6 个工具** `web_search`（Tavily）：有 key 才注册、`--no-web-search` 可关；免费额度 1000 credits/月，超额自动禁用 | 2026-09-21 |
| Q6 | **批次 8**：新增可选第 7 个工具 `web_fetch`（Firecrawl 精读，**单次最多 2 条 URL**）；`web_search` 加黑名单 + 时间预过滤 + 调研纪律提示词；两家额度账本抽 `CreditLedger` 基类 | 2026-09-21 |

---

## 跑一个真实任务

### 四种启动方式（逻辑只有一处）

```bash
sigma -p "任务"              # venv 里的 console script
python -m sigma -p "任务"    # 通用；console script 不在也能跑
sigma.bat -p "任务"          # Windows cmd，也可双击
.\sigma.ps1 -p "任务"        # PowerShell，也可右键“使用 PowerShell 运行”
```

`sigma.bat` / `sigma.ps1` 在项目根目录，**双击进交互模式**，不用先 activate venv。
四种入口都通向同一个 `sigma.cli:main`——入口可以有多个，组装只能有一个。

### 交互模式（连续聊，跨轮记得上下文）

```bash
sigma -i          # 或双击 sigma.bat
```

> ⚠ **Git Bash / mintty 里必须显式加 `-i`**。那些环境 stdin 不是 Windows 控制台句柄，
> 判不出"有人在敲键盘"。这不是偷懒：Windows 上 `isatty()` 对 NUL 设备**也返回 True**，
> 靠它判断会让 CI / 脚本里调用 `sigma` 静默进 REPL 卡住（实测复现过）。
> 取舍是：**宁可让人多打两个字符，也不要让 CI 挂住。**

### 输出是流式的

边跑边打印，不等任务结束。下面这段是真实跑出来的（DeepSeek `deepseek-chat`，2026-09-21）：

```
I'll search for OLD_VALUE in config.py.
⏺ grep({"pattern": "OLD_VALUE", "path": "config.py"})
  ✓ grep: 共 1 个匹配（1 个文件）： config.py:2:value = OLD_VALUE
`OLD_VALUE` 出现在 config.py 的第 2 行，该行完整内容为 `value = OLD_VALUE`。
[completed · 2 轮 · prompt 1629 / completion 40]
```

失败的工具画 `✗` 并**把失败原因写出来**——只画一个 ✗ 等于什么都没说，
而"模型看得到失败原因"是纠错能力的前提（详规 3.6）。

想先建一个能马上试的工作区：

```bash
python scripts/make_demo_workspace.py    # 在项目下建 demo/，含示例文件与可试任务
```

| 参数 | 说明 |
| --- | --- |
| `-p, --prompt` | 一次性模式的任务描述（与 `-i` 互斥） |
| `-i, --interactive` | 交互模式。**Git Bash / 管道里必须显式加它** |
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

> 📌 **工具集的当前边界**：核心五工具 `read` / `write` / `edit` / `bash` / `grep` 已全部就位；
> 另有**两个可选联网工具**——`web_search`（搜索，Tavily）与 `web_fetch`（精读，Firecrawl，
> **单次最多 2 条 URL**），只在解析到对应 key 时注册，`--no-web-search` 可整体关。
> 免费额度各 1000 **credits**/月，用尽后对应工具自动禁用；
> 低质量来源与超过 2 年的旧结果会在代码层被过滤，丢弃条数会写进结果里。
> **这一点写出来，不假装够用。**

> 🧪 **REPL 是简版**：交互模式**不支持中途打断 / 消息注入**（那需要 P3 的
> steering / follow-up 双队列）。一条任务跑完整轮才能输入下一条，
> 这一点写进启动横幅，不假装支持。

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

> ⚠️ **Windows 上有一个坑：用 `python -m pytest`，不要直接调 `.venv/Scripts/pytest.exe`。**
> 那个 `.exe` 是启动器，它从 **PATH** 解析解释器。如果 PATH 里 `python`
> 指向别的环境（本机实测会挑到 anaconda），pytest 就会用**那套** site-packages 跑，
> 症状是 `ModuleNotFoundError: No module named 'evals'` 这类**看起来像代码错**的报错。
> 正确写法：`./.venv/Scripts/python.exe -m pytest -q`（本次为此多排查了一轮）。
>
> ⚠️ **在沙箱化环境里跑 pytest，要把临时根指进仓库**（否则 pytest 的
> 临时目录会在系统 Temp 里越攒越多，见下）：
>
> ```bash
> export TMPDIR='C:/Users/<你>/Desktop/sigma/.pytest_cache/tmp'
> export PYTEST_DEBUG_TEMPROOT="$TMPDIR"
> ```
>
> **必须用 Windows 路径**：POSIX 形式（`/c/...`）会被 Python 忽略，静默回退到
> `AppData\Local\Temp`。另外 `--basetemp=` 没用——它自己就要删那个目录，照样被拦。
> 普通终端（无沙箱）不需要这一步，pytest 自己会清理。

**回放场景评测**（离线、免 key、不联网）：

```bash
python evals/runner.py                      # 跑全部场景，报告落 evals/reports/
python evals/runner.py --scenario read_then_edit
```

六个回放场景在 `tests/fixtures/transcripts/*.jsonl`
（清单与期望值在 `_scenarios.py`，**同一份清单**同时驱动门禁测试与运行器）。
`evals/datasets/` 的 70 条任务集**尚未落地**——那些需要真实 API key 与判定脚本。

**门槛注入实验**（验证"约束真的在生效"，而不只是"配置跑绿"）：

```bash
python scripts/gate_injection_batch1.py     # 期望 7/7
python scripts/gate_injection_batch15.py    # 期望 10/10
python scripts/gate_injection_batch24.py    # 期望 10/10
python scripts/gate_injection_batch6.py     # 期望 6/6
python scripts/gate_injection_batch7.py     # 期望 5/5
python scripts/gate_injection_batch8.py     # 期望 7/7
python scripts/gate_injection_p21.py        # 期望 4/4
python scripts/gate_injection_batch9.py     # 期望 3/3
python scripts/gate_injection_batch10.py    # 期望 4/4
python scripts/gate_injection_batch11.py    # 期望 4/4
python scripts/gate_injection_batch12.py    # 期望 4/4
```

每个脚本逐条对源码注入破坏 → 确认对应断言变红 → **还原**。
只有当"注入后确实变红"时该条才算通过——
**"注入做了、测试仍然绿"是失败**，那说明门槛没有在防它声称防的东西。
（批次 1 踩过 3 次这种情况，见 `P1-批次1-详规.md` 第 10.4 节。）

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
| `sigma_tools` | 内置工具：read / write / edit / bash / grep（+ 可选 web_search / web_fetch） |
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
