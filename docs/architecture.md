# sigma 架构方案

> 版本：v1.3 ｜ 2026-09-20
> 参照对象：Pi Agent Harness（架构与设计理念见 `pi-harness研究笔记.md`）
> 定位：自研 coding agent harness，目标是**可自证的设计**，不是功能数量
>
> **v1.1 变更**（依据本人 2026-09-20 的三条拍板，见 `docs/plans/P0-骨架.md` 第 6 节）：
> - a1 **产品壳范围对齐**：确认砍掉 Slack Bot / Web UI / RPC Mode（2.3 节）。
> - a2 **一律采用继承制（ABC）**：所有抽象接口/扩展点用 `abc.ABC`，
>   弃用 `typing.Protocol`。实体数据仍用 Pydantic `BaseModel`（4.1 / 4.2 / 4.3）。
> - a3 **首个 Provider = OpenAI 兼容协议**：P1 只做 **1 套** wire protocol，
>   Anthropic 推迟；2.2 节原「只做 2 套」表述已改为「先 1 后 1」（2.2 节）。
>
> **v1.3 变更**（2026-09-20 晚，依据 Pi 0.86.0 真实源码）：
> - **新增 4.0 节「消息模型：两层结构」**。替换掉原 v1.0 里
>   `Message.role` 只有三个值、且 `ContentBlock` 从未被定义的空洞。
> - agent 层 `AgentMessage`（ABC + 注册表）与 LLM 层 `LlmMessage`（四模型，不抽象）分离，
>   中间靠自由函数 `convert_to_llm()` 单向降级。
> - `role` 增加 `tool_result`；内容块显式定义四个模型。
> - 第 8 节新增四条 CI 门禁（消息层不混用 / 摘要降级位置 / 未知类型 / 不透明字段保真）。
> - 完整调研见 `docs/pi-harness研究笔记.md` **第 9.5 节**。
>
> 
> **v1.3 追加变更**（2026-09-20 晚，依据 P1 计划 Q0–Q4 拍板）：
> - **内置工具 4 → 5**：新增 `grep`（2.1 节保留清单、第 3 节架构图、
>   第 5.1 节预算表、第 11 节对照表）。理由：4 个内置工具覆盖不了
>   「先跨文件搜索定位、再修改」的任务形态，而这类任务在真实修 bug 场景里占比高。
> - **常驻区工具段预算 700 → 820 token**（5.1 节）。
>   常驻区小计 ≤ 3,500 不变（`grep` schema 约 120 token，在系统提示词余量内消化）。
> - **`truncate.py` 截断部分提前到 P1**（原属 P2，见 `docs/plans/P1-最小闭环.md` Q2）。
> - P1 评测仓库定为**自造迷你仓库**（Q0）；首个被测模型 DeepSeek `deepseek-chat`（Q3）。
> 
> 三条均有连带修正，逐条标记在对应节内。

---

## 0. 前置假设与阅读约定

### 0.1 本方案基于的三条假设

1. **sigma 是独立项目**，不与 `Desktop/inteview-agent` 合并。它自己就是一个完整的作品。
   → 若后续决定 sigma 替换 interview-agent 作为唯一主载体，第 9 节的排期可以整体压缩约 30%（因为不再需要在两个项目间切换上下文），但架构无需改动。
2. **Python 作为实现语言**（理由见 D1）。
3. **单个开发者，一学期约 13 周可用时间**，学期中 8 小时/周。

### 0.2 与被参照对象的关系

本方案**不复制 Pi 的代码**，只继承它的架构判断。原因不是技术性的，是定位性的：这个项目的价值在于"每一个设计决定都能被追问而不塌"。抄来的代码在追问面前是负资产。

因此下文凡引用 Pi 的做法，都标注为 **【参照】**，并说明我为什么改或为什么照抄。

---

### 0.3 两条贯穿全案的编码约定

1. **抽象用 ABC，数据用 BaseModel。**（依据 `AGENTS.md` 第 2、3 条，2026-09-20 拍板 a2）
   凡需要"多实现可替换"的位置——`Provider`、`Tool`、`Hook`、`Store`——都用
   `abc.ABC` + `@abstractmethod` 声明基类，由子类继承实现。
   **不用 `typing.Protocol`**：Protocol 是结构化类型，只做静态检查，不产生继承关系，
   与"采用继承制、后期扩展"的诉求不符（见 4.1 节的具体差异）。
   凡只承载数据、无行为的位置——`Message` 家族 / `Usage` / `ToolCall` / `ToolResult` /
   `ToolContext` / `StreamEvent`——用 Pydantic `BaseModel`。
   两者不冲突：**基类回答"谁是谁"，模型回答"装着什么"。**

   **一处需要按层判断的例外**：`AgentMessage`（4.0.3 节）**同时继承 `BaseModel` 和 `ABC`**。
   它属于 agent 层——既有数据（要落盘）又有行为（`to_llm()` 降级）、
   还需要运行时分派。**LLM 层的消息则一律纯 BaseModel，不做抽象。**
   区分标准不是"它是不是消息"，而是**"它有没有行为、要不要被统一持有"**。
   这条判断规则来自 Pi 的两层消息设计，详见 4.0 节与调研笔记 9.5 节。
2. **新引入的第三方依赖必须先验证它在 Python 3.12+ 上可用。**
   理由见 4.2 节：本项目依赖的第一批库（`typing_extensions`）在 3.13 上的
   stable 版本不导入可选模块，属于"装得上但用不了"的静默失败类型。

---

## 1. 决策清单

六项需要在写第一行代码之前拍板。每项给出推荐与理由。

### D1. 实现语言：Python

**推荐：Python。**

| 候选 | 论证 |
| --- | --- |
| **Python（推荐）** | ① 你的既有栈（FastAPI / Pydantic / pytest / import-linter）全部可直接复用，不新增语言成本<br>② 国内 AI Agent 应用工程岗位 Python 占绝对主导<br>③ Pydantic 的模型定义天然映射工具参数 schema，比 Pi 用的 typebox 更贴合你已有经验<br>④ interview-agent 的**评测方法论**可以直接迁移 |
| TypeScript | 只有一个优势：能直接读 Pi 源码。但代价是"新语言 + 新领域"同时学，8 小时/周的预算下这是净亏损 |

**必须接受的代价**：拿不到 Pi 的 jiti 式零构建热重载。Python 侧的热重载要自己设计，且有真实正确性陷阱（见 4.4 节）。这一项是本方案里技术难度最高的部分。

### D2. harness 边界：到会话管理为止，不做远程

**推荐：核心覆盖到「agent loop + 工具 + 会话 + 上下文」，CLI 只做薄壳。**

**做**：SDK 入口（`create_session()`）+ 交互式 REPL + 一次性模式（`-p` 输入输出）。
**不做**：RPC 协议、HTTP server、多实例编排、远程 session backend。

理由：Pi 的 RPC / Orchestrator / SQLite backend 本身还是实验能力，且那是"给产品用"的诉求。作品集需要证明的是"你懂 agent runtime 怎么设计"，不是"你支持多少种接入方式"。

**SDK 模式必须保留**，且优先级高于 CLI——它是最便宜的接口，同时它把"这是可复用的 harness，不是一个脚本"这件事变成可验证的事实（写一个 pytest 用 SDK 跑通即可证明）。

### D3. 扩展层：运行时状态，但只做工具级热重载

**推荐：扩展层是运行时可变状态；热重载范围限定在「工具注册表 + 钩子注册表」，不做任意代码自改写。**

这是本方案里**最需要克制的决定**。

Pi 的能力是"agent 改自己的扩展文件、jiti 热重载、当轮生效"。在 Python 里做同样的事，正确性风险集中在三处：

1. 模块级状态被重置或残留，行为不确定
2. 已注册的旧对象仍被某个长生命周期容器持有引用，表现为"改了代码但行为没变"
3. 重载过程中抛异常，注册表处于半更新状态

**作品集的正确策略是把它做小、做透、做可测**：让热重载只负责"按名解析的工具/钩子注册表整体替换"，把一个明确约束（调用方只能 `get(name)`，不得缓存实例）作为架构契约写进 CI。这样它可演示、可自动化验证、经得起追问。

而"agent 给自己写新工具"这个更亮眼的能力，**由工具写文件 + 手动触发热重载来实现**——效果上和 Pi 一致，但实现边界清晰。这个取舍要写进 README，它是一个有意的设计选择，不是能力缺失。

### D4. 上下文预算：常驻区 ≤ 3,500 token，且逐字节稳定

见第 5 节。

核心规则两条：

1. 常驻区（系统提示词 + 工具 schema + 项目说明 + 技能索引）**在整个会话中不得变化一个字节**，否则 prompt cache 从变化点起全部失效。
2. 一切动态内容只能 append 到消息尾部，或作为 tool_result 进入。

### D5. 安全边界：钩子软边界 + 影子 git checkpoint

**推荐：不宣称沙箱。**

钩子能挡的：字符串层面可识别的危险命令、越出工作区根目录的路径、写特定文件类型。
钩子**挡不住**的：`python -c` 里的任意代码、base64 编码后的命令、先写脚本再执行、网络外传数据。

因此真正起作用的是另外两层：

1. **工作区根目录约束** —— 所有写操作路径必须 resolve 后仍在 `workspace_root` 下。
2. **影子 git checkpoint** —— 每个写批次前自动提交一次，任何操作可整体回滚。
   实现上用独立的 `GIT_DIR`（放在会话目录下），**完全不碰用户仓库自身的 `.git`**，避免污染真实提交历史。

第 2 项是差异化的核心：Pi 把 git 检查点列为"使用者自己装配"的能力，这里把它做成核心。理由是它**可量化**（回滚成功率、恢复后成功率），而"权限弹窗拦住了"这种说法没法量化。

### D6. 不做容器隔离

Docker 在 Windows 上的开发体验和启动开销，对单人项目是纯负担。**明确不做，并在文档里写明"这是设计选择，代价是宿主环境不受保护"**——这句话比假装有隔离更专业。

---

## 2. 保留 / 改造 / 砍掉

对照 Pi 的能力清单。

### 2.1 保留（核心，逐项照做）

| 能力 | 为什么保留 |
| --- | --- |
| 薄 agent loop（4 阶段循环） | 全部价值的地基 |
| 5 个内置工具 read / write / edit / bash / grep | 构成最小闭环；`edit` 必须是**精确字符串替换 + 输出 unified diff**，不是整文件重写。**`grep` 为 2026-09-20 新增**（原 4 个覆盖不了「先跨文件搜索定位再改」的任务形态） |
| 会话树 JSONL（`id` + `parentId`） | 性价比最高的单项设计，成本极低、收益极大 |
| `beforeToolCall` / `afterToolCall` 钩子 | 有它才能把审批、路径保护、审计、结果改写全部外置 |
| 工具返回值两段式 `content` / `details` | 给模型的和给程序的分开 |
| steering / follow-up 双队列 | 人在回路最实用的形态 |
| 渐进披露的技能系统 | 上下文预算的主要手段 |
| 压缩对 prompt 有损、对磁盘历史无损 | 保证可逆性 |
| `AGENTS.md` 项目指令注入 | 上下文工程最直接的抓手 |
| 多 Provider 抽象 | 但要大幅简化，见 2.2 |
| 无 API key 也能跑核心测试 | 决定项目有没有回归测试 |
| 四种运行模式中的两种（交互 / 一次性） | 其余砍掉 |
| 供应链纪律、`--ignore-scripts` 一类的安装约束 | 见第 8 节 CI 契约 |

### 2.2 改造（保留思想，改变实现）

| Pi 的做法 | sigma 的做法 | 理由 |
| --- | --- | --- |
| 归一化 4 套 wire protocol（OpenAI Completions / OpenAI Responses / Anthropic Messages / Google Generative AI） | **先做 1 套（OpenAI 兼容），后做 1 套（Anthropic）；共 2 套封顶** | OpenAI 兼容协议一次性覆盖 DeepSeek / Kimi / GLM / 通义 / vLLM / Ollama / LM Studio，是投入产出比最高的一条。**2026-09-20 拍板（a3）：P1 只做 OpenAI 兼容这一套**，Anthropic 进 P4 之后，Google / Bedrock / Azure 永久不做 |
| 系统提示词压到 < 1,000 token | 起点定 **800 token，但作为可调参数** | Pi 的前提是"前沿模型已 RL 训练过编码任务"。你的评测集里要做系统提示词长度的 ablation，用数据定这个值，不要抄结论 |
| 默认无权限审批 | **钩子 + 工作区根约束 + checkpoint** 三层软边界 | 没有容器隔离就必须有替代品，checkpoint 是其中最可量化的一层 |
| `pi-tui` 差分渲染终端 UI | **纯文本 REPL + 流式打印** | TUI 是大量工作量、零架构信号，且会让 CI 极其难跑 |
| JSONL 树，无索引 | JSONL 追加写 + **内存索引，会话结束时可选落一份 stats** | 纯 JSONL 做检索会痛；但不要在 MVP 阶段引入数据库 |
| 扩展可以覆盖 harness 全部表面（含自定义编辑器、状态栏） | 扩展点限定为：**工具注册 + 钩子注册 + slash 命令** | 边界清晰才可测 |

### 2.3 砍掉（明确不做，且不接受中途追加）

```
MCP                  子代理              Plan Mode
待办追踪             后台 bash           Web UI / 编辑器集成
RPC / 远程 session    多实例编排          Slack Bot
SQLite session backend 主题系统          TUI 花哨渲染
权限弹窗 UI          15+ Provider        云端 sandbox
差分隐私 / 数据集共享  Anthropic（P1 不做，见 2.2）
```

**2026-09-20 拍板（a1）**：产品壳范围**与手绘架构图对齐**，即确认
**Slack Bot / Web UI / RPC Mode 三项都不做**。手绘图里的这一层是目标形态，
不是 P1–P4 的交付范围；架构留出了接口位（`core/sigma/sdk.py`），
但不实现任何一个接入端。

砍掉的每一条都要在 README 里给出"代替路径"（例如：要 MCP → 写一个扩展；要后台任务 → tmux），**照抄 Pi 的做法**。这是它最值得学的叙事方式：让"不做"看起来是设计而不是缺失。

---

## 3. 分层架构

五个包，依赖方向严格单向。**层与层之间用 import-linter 强制契约**（沿用 interview-agent 已验证的做法，见第 8 节）。

```
┌─────────────────────────────────────────────────────┐
│  sigma  (CLI + SDK)                                 │
│  组装依赖 · REPL · 一次性模式 · 会话加载             │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│  sigma_tools                                        │
│  内置工具实现：read / write / edit / bash / grep     │
│  （与扩展工具走同一条注册路径）                       │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│  sigma_session                                      │
│  会话树 JSONL · 上下文组装 · 压缩 · 资源加载          │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│  sigma_agent                                        │
│  Agent loop · 工具注册表 · 钩子总线 · checkpoint       │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│  sigma_ai                                           │
│  Provider 抽象 · 流式事件 · 用量统计 · 错误归一化      │
└─────────────────────────────────────────────────────┘
```

**这张竖排图不是依赖顺序。** `sigma_tools` 与 `sigma_session` 是**兄弟层**——
两者都只依赖 `sigma_agent` 与 `sigma_ai`，彼此之间没有任何依赖。
竖排只是为了排版。它们的依赖关系由第 8 节的 `independence` 契约强制，不是靠约定。

**关键设计点：`sigma_tools` 与扩展工具共用同一个注册表接口。** 这样"用扩展替换内置工具"是免费的，`--no-builtin-tools` + 只加载扩展能直接演示。Pi 做到了这一点，值得照抄。

**为什么 checkpoint 放在 `sigma_agent` 而不是 `sigma_tools`**：它需要感知"一个工具批次"的边界，这个边界只有 loop 知道。放在工具层会退化成"每个工具自己备份"，重复且不可控。

### 3.0 手绘架构图与本节的对齐关系

`docs/assets/pi-layered-architecture.png` 是本人的四层草图，**参照对象是 Pi，不是 sigma**。
图示内容（2026-09-20 逐项核对）：

| 图的层 | 图内条目 | 映射到 sigma |
| --- | --- | --- |
| 1 | Provider Registry · 事件流 · 消息变换 | `sigma_ai` |
| 2 pi-agent-core（循环引擎） | agentLoop · 工具执行管道 · Agent 状态管理 | `sigma_agent` |
| 3 pi-coding-agent（产品内核） | 会话树 · Compaction · Prompt 装配 · Extension / Skill | `sigma_session`（+ `sigma_tools` 的工具侧） |
| 4 产品壳 | CLI (TUI) · Slack Bot · Web UI · RPC Mode | `sigma` 的 CLI 与 SDK 入口；**后三项明确不做** |

**三处与本节不一致，逐条给出处置**：

1. **产品壳范围**：图里画了 Slack Bot / Web UI / RPC Mode，本方案 2.3 节砍掉了。
   **2026-09-20 拍板（a1）：对齐 → 确认砍掉。**
   两者的关系要说清：**图描述的是目标形态，2.3 节描述的是 P1–P4 的交付范围。**
   不是同一层的东西，所以不构成冲突。
2. **图的第 2、3 层标的是 pi 的包名**（`pi-agent-core` / `pi-coding-agent`），
   那是参照对象的命名，不是 sigma 的。sigma 对应 `sigma_agent` / `sigma_session`。
3. **本节的五层 ≠ 图的四层**：图把"工具"合进了第 2 层的"工具执行管道"，
   本方案把它拆成独立的 `sigma_tools` 层。拆的理由是
   **它要能与扩展工具共用一条注册路径，需要独立成为一层才谈得上契约**。

**另有一处必须更正**：早前我把这张图概括为"四层架构图"并逐层对标，
但**图里没有任何文字标明这四层之间的依赖方向**（只有向下箭头表示排列顺序）。
所以拿它去论证 `sigma_tools` 与 `sigma_session` 的依赖关系是无效的——
那张图**根本不含这个信息**。
兄弟层这个结论来自本方案第 8 节的依赖边表，并由 `independence` 契约强制，
不是从图里读出来的。

### 3.1 目录结构

```
sigma/
├── pyproject.toml                 # 打包依赖 + [tool.importlinter] 分层契约
├── .github/workflows/ci.yml       # CI：lint-imports / mypy / pytest
├── AGENTS.md                      # 本仓库自己的项目指令（自举：sigma 读它）
├── README.md
├── core/
│   ├── sigma_ai/
│   │   ├── base.py                # BaseProvider（ABC）
│   │   ├── messages.py            # LLM 层：四个消息模型 + 内容块（4.0.1 / 4.0.2）
│   │   ├── events.py              # StreamEvent 判别联合
│   │   ├── openai_compat.py       # OpenAI 兼容实现（P1 唯一的 provider）
│   │   ├── anthropic.py           # P4 之后
│   │   └── fake.py                # 确定性回放（见 7.2）
│   ├── sigma_agent/
│   │   ├── base.py                # BaseLoop / BaseTool（ABC）
│   │   ├── agent_messages.py      # agent 层：AgentMessage（ABC）+ 注册表 + convert_to_llm（4.0.3–4.0.6）
│   │   ├── loop.py                # AgentLoop —— BaseLoop 的唯一子类
│   │   ├── registry.py            # 工具注册表 + 热重载
│   │   ├── hooks.py               # 钩子总线（BaseHook ABC）
│   │   ├── checkpoint.py          # 影子 git
│   │   └── types.py               # ToolDefinition / ToolResult / ToolCall
│   ├── sigma_session/
│   │   ├── base.py                # BaseStore（ABC）
│   │   ├── tree.py                # SessionTree
│   │   ├── store.py               # JSONL 追加读写
│   │   ├── context.py             # 上下文组装 + 预算
│   │   ├── compact.py             # 压缩
│   │   └── resources.py           # AGENTS.md / skills 发现
│   ├── sigma_tools/
│   │   ├── read.py  write.py  edit.py  bash.py    # 各含一个 BaseTool 子类
│   │   └── truncate.py            # 输出截断（见 5.3）
│   └── sigma/
│       ├── cli.py                 # 薄壳
│       └── sdk.py                 # create_session()
├── extensions/                    # 运行时加载，样例扩展放这里
├── evals/                         # 评测（见第 7 节）
│   ├── datasets/{reproduce,synthetic,adversarial}/
│   ├── runner.py
│   ├── judges/
│   └── reports/
├── tests/                         # 单测，无 API key 全绿
│   └── fixtures/arch/{clean,violating}/   # 契约测试的正反两个样例
└── docs/
    ├── architecture.md            # 本文件
    ├── pi-harness研究笔记.md       # 调研来源
    ├── assets/                    # 架构图
    ├── plans/                     # 各阶段实施计划与验收证据
    └── decisions/                 # 每个决策一份 ADR
```

**读这棵树时注意**：包内的 `.py` 文件（`protocol.py` / `loop.py` / `tree.py` 等）
是**规划中的文件**，P0 阶段尚未创建。
P0 实际只落地了：五个包的 `__init__.py`、一个占位 `cli.py`、
契约测试与 fixture、`pyproject.toml`、CI、以及文档。

**分层契约的位置**：写在 `pyproject.toml` 的 `[tool.importlinter]` 里，
**没有独立的 `importlinter.toml`**——实测 import-linter 2.14 从 `pyproject.toml` 读取。

**注意 `AGENTS.md`**：sigma 自己就是一个会被 agent 操作的仓库，所以它应该有一份自己的 `AGENTS.md`。**用自己的工具改自己的代码，是唯一有说服力的端到端验证**——这件事要写进 README 作为验收场景。

---

## 4. 核心接口设计

以下签名是可直接实现的粒度。

### 4.0 消息模型：两层结构（2026-09-20 新增）

> **来源**：本节依据 **Pi 0.86.0 的真实源码**（解包 npm 读 `.d.ts` 与 `.js`），
> 完整调研见 `docs/pi-harness研究笔记.md` 第 9.5 节。
> **本节替换掉了原 v1.0 里 `Message.role` 只有三个值、且 `ContentBlock` 从未被定义的那个空洞。**

**核心结构：消息分两层，中间一个单向降级函数。**

```
agent 层    AgentMessage        开放联合：LLM 消息 + 应用自定义消息
                │
                │  convert_to_llm()      ← 单向降级，唯一通道
                ↓
LLM 层      LlmMessage          封闭联合：system / user / assistant / tool_result
```

**这两层不是同一个东西，混为一层是本设计最容易犯的错误。**
会话里存的东西**不等于**发给模型的东西。两者之间隔着一个显式转换，
于是"存了 UI 专属消息但模型看不见"是免费得到的，不需要额外机制。

#### 4.0.1 LLM 层：四个具体模型，没有基类

**这一层不做抽象。** 它严格对应 wire protocol，字段是协议要求的样子，
加基类只会让协议转换更难写。

```python
class SystemMessage(BaseModel):
    role: Literal["system"] = "system"
    content: str | list[TextBlock]
    # 提示词段落的具名增删改。见 4.0.4
    sections: dict[str, str | None] | None = None
    tools_added: list[ToolMeta] | None = None
    tools_removed: list[str] | None = None      # 仅工具名
    timestamp: int

class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    content: str | list[ContentBlock]
    timestamp: int

class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: list[ContentBlock]          # TextBlock | ThinkingBlock | ToolCallBlock
    api: str = ""
    provider: str = ""
    model: str = ""
    response_id: str = ""
    usage: Usage
    stop_reason: StopReason
    error_message: str = ""
    timestamp: int

class ToolResultMessage(BaseModel):
    role: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    content: list[ContentBlock]
    details: dict[str, Any] = {}         # 不进上下文（见 4.2 节）
    is_error: bool = False
    timestamp: int

LlmMessage = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
```

**三条必须写进实现约定的点**：

1. **`role` 是四个值，`tool_result` 是独立角色。**
   工具结果**不是**塞在 assistant 消息里的内容块——它有自己的消息类型、
   自己的 `tool_call_id`。这与 OpenAI 兼容协议一致，转换时少一层映射。
2. **LLM 消息在降级时原样透传，不重新构造。**
   `AssistantMessage` 里那几个 provider 私有字段（见 4.0.2 的 `*_signature`）
   如果被重建一次就会丢，症状是多轮对话里模型行为异常且极难定位。
3. **`details` 不进上下文**——与 4.2 节 `ToolResult` 的规则同一套。

#### 4.0.2 内容块：四个模型，靠 `type` 判别

```python
class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str
    text_signature: str | None = None    # provider 返回，必须原样回传

class ThinkingBlock(BaseModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    thinking_signature: str | None = None
    redacted: bool = False               # 被安全过滤器遮蔽

class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    data: str                            # base64
    mime_type: str

class ToolCallBlock(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None

ContentBlock = TextBlock | ThinkingBlock | ImageBlock | ToolCallBlock
```

**三个 `*_signature` 字段是同一个模式：provider 返回的、必须原样回传的不透明串。**
这是自研 harness 最容易漏的东西——漏了会让多轮对话里模型行为异常，
而且**症状完全不指向根因**。P1 就要把它们放进类型，哪怕 P1 用不到。

**`redacted` 字段同理**：Anthropic 的 thinking 块被安全过滤器遮蔽时，
不透明加密载荷存在 `thinking_signature` 里，必须回传才能维持多轮连贯性。

#### 4.0.3 agent 层：ABC + 运行时注册

**这一层要做抽象**，理由与 LLM 层相反：它**有行为**（降级方法）、
需要被 loop 与会话树统一持有、需要运行时分派。

```python
class AgentMessage(BaseModel, ABC):
    """agent 层消息抽象基类。

    继承 BaseModel 是为了序列化（会话树要落盘 JSONL）；
    继承 ABC 是为了强制子类实现 to_llm()。
    """

    timestamp: int

    @abstractmethod
    def to_llm(self) -> LlmMessage | None:
        """降级成 LLM 层消息。

        返回 None 表示这条消息不进模型上下文
        （例如 UI 专属通知、被 exclude_from_context 标记的 bash 记录）。
        """
        raise NotImplementedError
```

**注册表 P1 就要有**，否则 P2 加压缩摘要消息类型时要回头改核心：

```python
_MESSAGE_TYPES: dict[str, type[AgentMessage]] = {}

def register_message_type(cls: type[AgentMessage], *, role: str) -> type[AgentMessage]:
    """扩展用这个函数挂载自己的消息类型。

    对应 Pi 的 `declare module`（declaration merging），
    但 Pi 是编译期被动填充，这里是运行期主动注册。
    代价是失去编译期穷尽性检查，收益是不用改核心包、且可在运行时注册
    ——与 D3 节「扩展层是运行时可变状态」更契合。
    """
    if role in _MESSAGE_TYPES:
        raise DuplicateMessageType(role)     # 不允许静默覆盖
    _MESSAGE_TYPES[role] = cls
    return cls
```

**P1 只要两个子类**：`LlmMessageWrapper`（包住四个 LLM 模型）和
`ToolResultAgentMessage`。压缩摘要、分支摘要留到 P2——**但注册表和
`DuplicateMessageType` 报错规则 P1 落地**。

#### 4.0.4 `SystemMessage` 承担提示词演进与工具集变更

这是 Pi 的一个关键设计，**直接决定了会话树 + 压缩那套能不能成立**：

- **首条 system 消息是基线提示词**；之后的 system 消息在改它。
- `content` 追加指令，`sections` 按名替换或删除段落（`None` 表示删除）。
- `tools_added` / `tools_removed` 改变该点之后的工具集。
- **按顺序重放所有 system 消息，就得到当前提示词与当前工具集。**

**收益**：系统提示词的演进被**记录成消息**，而不是一个被覆盖的变量。
所以"这条消息是在哪套提示词下产生的"永远可追溯——评测与 ablation 分析都需要这个。

**代价**（必须写进文档，不藏）：provider 侧要处理"对话中途的 system 消息"。
OpenAI 兼容协议接受中途 system 消息则原位发；不接受的则需按重放状态
重建首条 system 消息。**这条转换逻辑是 P1 就要写的，不能推到后面。**

**与 D4 节的张力**：D4 要求常驻区逐字节稳定。中途插入 system 消息会**破坏 prompt cache**。
处置方式：**P1 只在会话开始处发一条 system 消息**（等价于稳态），
中途变更提示词的能力**保留类型但不启用**，直到 P2 做压缩时一并设计缓存策略。

#### 4.0.5 `convert_to_llm()`：自由函数，不是 `Context` 的方法

**改为自由函数**（原 v1.0 写作 `context.to_messages()`）。理由：它有**三个调用方**
——常规请求、压缩摘要生成、扩展自定义——做成 `Context` 方法会让后两者被迫构造一个假 Context。

```python
def convert_to_llm(messages: list[AgentMessage]) -> list[LlmMessage]:
    """agent 层 → LLM 层。唯一通道。

    四条规则（照抄 Pi 的 convertToLlm 设计）：
    1. LLM 消息原样透传，不重新构造（避免丢 *_signature 类字段）
    2. agent 层自定义消息统一降级成 user 消息，不新造协议 role
    3. exclude_from_context 的消息显式丢弃（返回 None）
    4. 认不出的类型 —— 见下面的警告
    """
```

**第 4 条是本节最重要的一处「不照抄」**：

| Pi 的做法 | sigma 的做法 |
| --- | --- |
| 认不出的 role 返回 `undefined`，被 `filter` 静默丢弃 | **记录 warning 并按原样透传，或直接抛错** |

**理由**：Pi 的联合类型在**编译期**已封闭，`default` 分支理论上不可达，
所以运行时的静默是安全的兜底。**Python 没有编译期穷尽性检查**——
静默丢弃会变成"消息莫名消失"且无从排查。

这与 4.4 节「不允许扩展静默覆盖内置工具」是同一条原则：
**宁可崩，不要错。**

#### 4.0.6 压缩摘要：降级成 user 消息，不是 system

Pi 用常量前缀后缀把摘要包成 `<summary>` 标签：

```python
COMPACTION_SUMMARY_PREFIX = (
    "The conversation history before this point was compacted "
    "into the following summary:\n\n<summary>\n"
)
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"
BRANCH_SUMMARY_PREFIX = (
    "The following is a summary of a branch that this conversation came back from:"
    "\n\n<summary>\n"
)
BRANCH_SUMMARY_SUFFIX = "</summary>"
```

**三个要点**：
1. **用 `<summary>` 标签包裹**，让模型明确区分"这是摘要"与"这是用户说的话"。
   分支摘要的前缀还交代**来源**，让模型知道上下文里为什么少了一段。
2. **前缀是常量导出**，压缩端与转换端共用同一字符串，不会漂移。
3. **摘要降级成 `user` 消息，不是 `system`**——不占常驻区，
   **且不会破坏 prompt cache**。这条与 D4 节规则一致，是本设计的必要前提。

#### 4.0.7 本节对既有内容的修订清单

| 位置 | 原文 | 修订后 |
| --- | --- | --- |
| 4.1 节代码块 | `Message.role: Literal["system","user","assistant"]`（三个值） | 拆分为四个 LLM 模型，加 `tool_result` |
| 4.1 节代码块 | `content: list[ContentBlock]`（类型未定义） | `ContentBlock` 四模型判别联合，见 4.0.2 |
| 4.1 节 | `Provider.stream(messages: list[Message], ...)` | 参数类型改为 `list[LlmMessage]`——**provider 只认 LLM 层** |
| 4.3 节 | `messages = self.ai.to_messages(ctx)` | `messages = convert_to_llm(ctx.messages)` |
| 4.3 节 | `session.append(EntryKind.tool_result, {...})` | 追加 `ToolResultMessage`（agent 层），降级由 4.0.5 负责 |
| 第 8 节门禁 | 无 | 新增「消息层不混用」契约，见下 |

**新增 CI 门禁**：

| 门禁 | 断言内容 |
| --- | --- |
| 消息层不混用 | `sigma_ai` 不得 import `sigma_agent` 的 `AgentMessage`；`convert_to_llm` 是 agent 层→LLM 层的唯一引用点 |
| `SystemMessage` 字段集合 | 单测断言其字段**恰好**为约定集合，新增字段即失败——防「把摘要塞进 system 消息」（4.0.6 节） |
| 摘要降级位置 | 单测断言压缩摘要经 `convert_to_llm` 后 `role == "user"`，不是 `system` |
| 未知消息类型 | 单测断言未知类型**不被静默丢弃**（记录 warning 或抛错） |

**第一条同时是 import-linter 契约**：`sigma_ai` 已经是依赖图的最底层，
不允许它认识 `AgentMessage`。这条契约**机器可查**，不是靠约定。

### 4.1 Provider 抽象（sigma_ai）

> **2026-09-20 变更（a2）**：`Provider` 由 `Protocol` 改为 `ABC`。
> 这是全案第一个、也是影响面最大的继承制改造点。
> **同时按 4.0.7 节修订**：`stream` 与 `estimate_tokens` 的参数类型改为 `list[LlmMessage]`。

```python
from abc import ABC, abstractmethod

# ---- 数据载体：Pydantic BaseModel（AGENTS.md 第 3 条）----
# 消息模型见 4.0 节。此处只列 provider 层直接依赖的部分。

class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0

# ---- 抽象基类：ABC（AGENTS.md 第 2 条，a2 拍板）----

class BaseProvider(ABC):
    """所有 Provider 的抽象基类。

    子类必须实现 stream 与 estimate_tokens。
    抽象方法一律不得提供默认实现——默认实现会让"忘了实现"变成静默错误。

    注意参数类型是 LlmMessage（4.0.1 节），不是 AgentMessage。
    provider 层永远不认识 agent 层的消息——这条由第 8 节的 import-linter 契约强制。
    """

    @abstractmethod
    def stream(
        self,
        messages: list[LlmMessage],
        tools: list[dict[str, Any]],
        *,
        model: str,
        signal: CancelToken,
    ) -> AsyncIterator[StreamEvent]:
        """流式产出统一事件。子类不得自行定义事件类型。"""
        raise NotImplementedError

    @abstractmethod
    def estimate_tokens(self, messages: list[LlmMessage]) -> int: ...
```

**为什么是 ABC 而不是 Protocol**（a2 的直接后果，必须能讲清）：

| 维度 | `typing.Protocol` | `abc.ABC`（本项目选用） |
| --- | --- | --- |
| 类型关系 | 结构化：只要方法签名对得上就算"实现"，**不产生继承关系** | 名义化：必须显式继承 |
| 忘实现方法 | 静态检查器报错，运行期直接拿到 `AttributeError` 或静默走错分支 | **实例化即抛 `TypeError`**，无法带着缺失实现跑起来 |
| 新增方法时的扩散 | 所有"碰巧签名匹配"的类自动变成子类，改动影响面不可见 | 抽象方法未实现 → 全部子类立刻报错，改动可见 |
| 与 `AGENTS.md` 第 2 条 | 冲突（那一条要求继承制） | 一致 |
| 注册机制 | 无 | `BaseProvider.register(X)` 可注册非继承的第三方实现（留后门） |

关键区别在第 3 行：**ABC 让"忘了实现"在实例化时就暴露，Protocol 会把问题推到第一次调用。**
对"后期扩展"这个诉求，早暴露价值远大于晚暴露——这正是 `AGENTS.md` 第 2 条要继承制的实际收益。

顺序三必须写进实现约定的点：

1. **`StreamEvent` 必须是一个完整的判别联合**（文本增量 / 工具调用增量 / 用量 / 结束原因 / 错误），UI、落盘、测试**消费同一个类型**。不要在 UI 层另造一套事件。
2. **`estimate_tokens` 只用于预算预警，不作为账本。** 真实用量以 provider 返回的 `usage` 为准，本地估算要持续用真实值做比例校准。自己实现 tokenizer 当唯一依据是错的。
3. **错误归一化**：把各家的错误码映射到统一的 `ProviderError`（`rate_limit` / `context_overflow` / `auth` / `transient` / `invalid_request`）。`context_overflow` 要能被上层捕获并触发压缩——这是压缩的两条触发路径之一。

### 4.2 工具定义（sigma_agent）

> **2026-09-20 变更（a2）**：原设计的 `ToolDefinition` 里塞 `fn: Callable`，
> 是函数式写法，与继承制冲突。改为**「工具基类 + 轻量元数据」**两段式：
> 行为写在 `BaseTool` 的抽象方法里，`ToolDefinition` 只留元数据。

```python
from abc import ABC, abstractmethod

# ---- 行为：ABC 基类（AGENTS.md 第 2 条）----

class BaseTool(ABC):
    """所有工具（内置 / 扩展）的抽象基类。二者走同一条注册路径。"""

    name: str = ""
    description: str = ""
    read_only: bool = False       # 只读工具允许并发执行
    needs_approval: bool = False

    @property
    @abstractmethod
    def params(self) -> type[BaseModel]:
        """参数模型。Pydantic 模型 → 自动生成 JSON Schema。"""
        raise NotImplementedError

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行。单个工具失败必须返回 is_error=True 的 ToolResult，
        不得向上抛异常——见 4.3 节第 3 点。"""
        raise NotImplementedError

# ---- 元数据：BaseModel（AGENTS.md 第 3 条）----

class ToolDefinition(BaseModel):
    """注册表里存的东西。只装元数据，不装可执行引用。"""
    name: str
    description: str
    params_schema: dict[str, Any]     # 由 BaseTool.params 生成的 JSON Schema
    tool: BaseTool                    # 指向基类实例，不是裸函数
    read_only: bool = False
    needs_approval: bool = False
    source: str = "builtin"

class ToolResult(BaseModel):
    content: list[ContentBlock]          # 进上下文，给模型看
    details: dict[str, Any] = {}         # 不进上下文，给 UI / 审计 / 评测
    is_error: bool = False

class ToolContext(BaseModel):
    session_id: str
    workspace_root: Path
    signal: CancelToken
    emit: Callable[[str], None]          # 流式进度回调
```

**这个改动的连带收益**：`params` 从「实例字段」变成「属性 + 抽象方法」后，
内置工具与扩展工具无法再通过"传不同函数"来偷懒，必须真的各写一个类。
这让 4.4 节「扩展与内置同路径注册」这条约束有了结构化载体——
`registry.register(tool: BaseTool)` 的签名本身就限制了能注册什么。

`params` 用 Pydantic 模型而不是手写 JSON Schema，是 Python 侧相对 Pi 的**真实优势**——它同时给了你参数校验、类型提示和 schema 生成。要用足。

`details` 不进上下文这一点必须严格执行：它是最容易被滥用的字段。**判定依据：如果一段内容不需要模型看到，它就应该在 `details` 里。** 这直接决定上下文预算。

### 4.3 Agent loop（唯一的循环实现）

> **2026-09-20 变更（a2）**：`AgentLoop` 改为继承 `BaseLoop`。
> 让 loop 也有基类，是为了给"换一种循环策略"留出继承位——
> 但**当前只允许一个子类**（`AgentLoop`），不得并行存在第二个实现。

```python
class BaseLoop(ABC):
    """agent 循环的唯一抽象。当前阶段只有一个子类。"""

    @abstractmethod
    async def run_turn(self, session: SessionTree, queues: Queues) -> TurnResult: ...

class AgentLoop(BaseLoop):
    async def run_turn(self, session: SessionTree, queues: Queues) -> TurnResult:
        while True:
            ctx = self.session.build_context(budget=self.budget)
            ctx = await self.hooks.transform_context(ctx)
            # 4.0.5 节：自由函数，不是 Context 的方法。
            # 会话里存的是 AgentMessage，发给模型的是 LlmMessage。
            messages = convert_to_llm(ctx.messages)

            text, calls, usage = await self._stream_model(messages)

            if not calls:
                return TurnResult.completed(text, usage)

            await self.checkpoint.mark(label=f"batch:{len(calls)}")

            results = await self._execute_batch(calls)      # 见下
            for call, result in results:
                # 存的是 agent 层消息（4.0.3 节）。降级由 convert_to_llm 负责。
                session.append(ToolResultAgentMessage.from_result(call, result))

            if await self.hooks.should_stop_after_turn(session):
                return TurnResult.stopped(...)
```

**为什么 loop 也要基类、却又明确禁止第二个实现**：这是 `AGENTS.md` 第 2 条
（"采用继承制，后期扩展"）与架构方案第 4.3 节（"唯一的循环实现"）之间的一处张力。
处理方式是把这两句话都写进代码约束：

- 留基类 → 未来若要做"两阶段规划循环"或"反思循环"，不必动公共接口。
- 只允许一个子类 → 用测试断言 `BaseLoop.__subclasses__()` 恰好只有 `AgentLoop`。
  这条断言在 P1 落地，防止中途冒出"另一套简化 loop"。

`_execute_batch` 是必须写对的三个点：

```python
async def _execute_batch(self, calls: list[ToolCall]) -> list[tuple[ToolCall, ToolResult]]:
    # 1. 只读工具并发，写工具严格顺序（写工具并发是不确定性的来源）
    # 2. 每个调用都要过钩子，钩子可以阻断并返回替代结果
    # 3. 单个工具失败不中断批次，转成 is_error=True 的 ToolResult
    #    —— 让模型自己看到失败原因并纠错，比抛异常好
```

第 3 点要写进实现约定。**工具失败必须变成模型可见的信息，而不是进程异常**。这是"agent 能自我纠错"的前提，也是评测里"首次成功率 vs 最终成功率"这个指标能拉开差距的原因。

**关于 steering 的送达时机**（照抄 Pi，因为它是对的）：

| 队列 | 送达时机 | 语义 |
| --- | --- | --- |
| steering | 当前 assistant turn 的工具批次执行完后 | **打断本批次剩余工具**，立即插入 |
| follow-up | `should_stop_after_turn` 返回 True 之后 | 等 agent 自认完成再送达 |

实现要点：REPL 需要在 agent 运行期间非阻塞读 stdin（后台线程塞队列即可）。**不要为此引入 TUI 框架。**

### 4.4 工具注册表与热重载（技术难度最高的部分）

```python
class ToolRegistry:
    """所有工具（内置 / 扩展）走同一路径注册。

    热重载正确性的唯一硬约束：
    调用方只能通过 get(name) 取工具，不得缓存 ToolDefinition 实例。
    违反此约束的症状是"改了代码但行为没变"。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._source_of: dict[str, str] = {}

    def register(self, tool: BaseTool, *, source: str = "builtin") -> None:
        """签名接受 BaseTool 而不是裸 Callable——
        这一条让"扩展工具与内置工具同路径"成为类型层面的约束，不是约定。"""
        ...

    def get(self, name: str) -> BaseTool:
        return self._tools[name].tool   # 每次查表，不返回缓存引用

    def reload_source(self, source: str) -> ReloadReport:
        ...
```

热重载的模块加载必须这样做：

```python
def _import_fresh(path: str) -> ModuleType:
    """每次生成新的 module 对象。

    不要用 importlib.reload()：它保留同一个 module 对象，
    其它模块以 `from x import y` 形式持有的旧引用不会被更新。
    """
    name = f"sigma_ext_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
```

**必须处理的三个失败场景**（每个都要有单测）：

1. 新模块导入时抛异常 → 保留旧注册表，返回失败报告，不进入半更新状态
2. 新模块没有注册任何工具 → 视为扩展被移除，清理旧注册项（而不是保留）
3. 新模块注册了与内置工具同名的工具 → `DuplicateToolError`，拒绝并保留旧状态

第 3 点是刻意的：**不允许扩展静默覆盖内置工具**。要替换必须显式走 `--no-builtin-tools`。静默覆盖是最难排查的一类 bug。

---

## 5. 上下文预算

### 5.1 分区

| 分区 | 预算（token） | 加载方式 | 可变性 |
| --- | --- | --- | --- |
| 系统提示词 | ≤ 800 | 启动时固定 | 会话内不可变 |
| 工具 schema（5 个） | ~820 | 启动时固定 | 会话内不可变 |
| 工具 schema（+3 可选） | +~450 | 启动时按 flag | 会话内不可变 |
| `AGENTS.md` | ≤ 1,500 | 启动时注入，硬截断 | 会话内不可变 |
| 技能索引（name + description） | ≤ 600 | 启动时固定 | 会话内不可变 |
| checkpoint / 摘要 | ≤ 1,000 | 压缩产出 | 随压缩更新 |
| **常驻小计** | **≤ 3,500** | | **逐字节稳定** |
| 会话历史 + 工具输出 | 剩余 | 动态 | append only |

### 5.2 两条硬规则

**规则一：常驻区在整个会话中不得变化一个字节。**

这是 prompt cache 的充要条件。推论：

- 任何"根据任务类型动态调整系统提示词"的设计都是错的
- 时间戳、随机 ID、会话元信息**不能进系统提示词**
- 技能正文**不能**插在常驻区，只能作为 tool_result 或消息 append 到尾部

**规则二：动态内容只能尾部追加。**

如果 RAG 或技能加载需要"插入"到中间位置，缓存从插入点起全部失效。正确做法是把检索结果放在**当前轮的消息尾部**，而不是拼接进历史。

这两条要在代码里做成断言，不是靠自觉：`Context` 对象在会话开始时冻结常驻区哈希，每轮 build 时校验，不一致直接抛异常。**这个断言的成本是零，收益是它把一条容易违反的约定变成不可违反的约束。** 这是评测里"prompt cache 命中率"指标的前提。

### 5.3 工具输出截断（最容易爆预算的地方）

一条 `bash` 命令可以吐出 10 万 token。**这不是优化问题，是必须有的设计。**

三元策略：

```
输出 ≤ 8 KB        → 完整进上下文
8 KB < 输出 ≤ 256 KB → 落盘到会话临时目录，上下文里只放：
                      头部 40 行 + 尾部 40 行 + 总行数 + 文件路径 + 提示"可用 read 查看完整内容"
输出 > 256 KB      → 落盘并拒绝进入上下文，只返回路径 + 行数 + 退出码
```

`read` 工具本身也必须支持行范围参数，否则 agent 无法读取被截断的内容——**截断和分页是配套的，只做截断不做分页会让 agent 卡死**。

`details` 里保存完整输出的路径和字节数，评测框架据此统计"截断发生率"。这个比例本身就是一个值得报告的指标。

---

## 6. 安全边界

### 6.1 三层，按可靠性排序

| 层 | 机制 | 可靠性 | 可量化指标 |
| --- | --- | --- | --- |
| L1 | 工作区根目录约束（写路径 resolve 后必须在 root 下） | 高，可证明 | 越界拦截率、误拦率 |
| L2 | 影子 git checkpoint（每批次前提交，可整体回滚） | 高，可证明 | 回滚成功率、恢复后成功率 |
| L3 | 钩子规则（危险命令匹配） | **低，只能减少不能消除** | 对抗集拦截率、误拦率 |

### 6.2 影子 git 的实现约定

```
GIT_DIR   = <session_dir>/shadow.git
GIT_WORK_TREE = <workspace_root>
```

关键点：

- **不碰用户仓库自己的 `.git`。** 用独立 `GIT_DIR` 是唯一的正确做法，`git stash` 和往用户历史里插 commit 都会污染真实项目。
- 需要读取工作区的 `.gitignore` 作为 exclude 源，否则会把 `node_modules` 之类提交进去，checkpoint 会慢到不可用。
- 回滚必须处理**文件删除**：`checkout <commit> -- .` 不会删除新增文件，需要配合清理。这是最容易漏的一条，要有单测。
- 大文件要设上限（例如单文件 > 5 MB 不入 checkpoint），否则 checkpoint 会成为性能瓶颈。

### 6.3 钩子挡不住什么（写进文档，不藏）

```
✗  python -c "任意代码"          → 字符串匹配无法解析语义
✗  base64 / 变量拼接后的命令      → 同上
✗  先写一个脚本文件再执行          → 拆成两步后每步都"看起来合法"
✗  通过合法工具外传数据            → 网络请求在 bash 里
✗  对工作区外的读操作              → L1 只约束写
```

**在 README 里明确写这一段。** 一个声称"有安全防护"但经不起追问的设计，扣分远大于"明确声明了边界在哪"的设计。这一段本身就是设计成熟度的证据。

---

## 7. 可验证性设计

**这是本方案里最重要的章节。** interview-agent 的教训是：骨架全部到位、实现没有接上，最后没有任何一个论断能被数据支撑。

### 7.1 评测集：三类来源，共约 70 条

| 类型 | 数量 | 构造方式 | 判定 |
| --- | --- | --- | --- |
| A 复现类 | 20 | 从真实小仓库取一个已修复的 bug，checkout 到修复前，任务描述取自 issue | 指定测试用例通过（exit 0） |
| B 合成类 | 30 | **先写测试并验证它在当前代码上失败，再写任务描述** | 同 A |
| C 对抗类 | 20 | 诱导 agent 执行越界 / 危险操作 | 是否被拦截 / 是否未越界 |

**B 类的构造顺序是硬性的**：必须先有失败测试、后有任务描述。反过来做（先写实现再补测试）会造出"自己的实现恰好能过"的任务，评测就失去意义。这是最容易自欺的地方。

**C 类不是任务完成度评测，是边界评测**，只统计拦截率和误拦率，不进成功率指标。

### 7.2 确定性回放（可测试性的地基）

用 `FakeProvider` 回放录制的 transcript，让 agent loop 的测试**完全不需要 API key、完全确定性**。

```
tests/fixtures/transcripts/
  read_then_edit.jsonl
  bash_fail_then_retry.jsonl
  context_overflow.jsonl
  tool_error_recovery.jsonl
```

实现方式：`FakeProvider` 按顺序吐出 `StreamEvent`，并支持"当输入上下文哈希匹配时返回录制的事件"。这样：

- CI 里 agent loop 的回归测试可跑
- 钩子、批处理顺序、截断、压缩的边界都能被覆盖
- 不需要联网、不需要花钱

**这条是"项目能不能长期演进"的分水岭。** 一个 agent 项目如果所有测试都要真跑模型，那么它一定没有回归测试，进而一定会在某个阶段退化。

### 7.3 基线（三层）

| 编号 | 配置 | 用途 |
| --- | --- | --- |
| B0 | 纯 LLM，无工具，单轮 | 证明"任务本身不能靠模型一次答对" |
| B1 | 有工具，无 harness 干预：满上下文、无压缩、无钩子、无 checkpoint | 证明"工具本身不等于 harness" |
| B2 | 完整 sigma（默认配置） | 主结果 |
| B3 | B2 + 一个扩展 | 证明扩展层真的能改变行为 |

**B1 是整份报告里最关键的一条对照。** 如果没有 B1，"harness 有用"这个论断就是空的——因为你不知道收益来自工具，还是来自你的设计。

### 7.4 Ablation 矩阵

每次只关一个组件，量化它的边际贡献。

| 关闭项 | 对照 | 关注的指标 |
| --- | --- | --- |
| 压缩 | 无压缩、超限直接失败 | 长任务成功率、token |
| 会话树（退化为线性追加） | 树 + 压缩 | 分支任务的成功率、上下文 token |
| 系统提示词 800 → 5,000 | 800 版本 | 成功率、常驻 token |
| 工具 8 → 5（去掉 find/ls/其他） | 8 工具版本 | 成功率、工具调用轮数 |
| steering | 无 steering | 人为注入一次错误方向后的恢复率 |
| checkpoint | 无 checkpoint | 破坏性任务后的恢复率 |
| 技能全量注入 vs 按需 | 按需版本 | 常驻 token、cache 命中率 |
| 只读工具并发 vs 全顺序 | 并发版本 | wall-clock |

### 7.5 指标定义

| 指标 | 定义 |
| --- | --- |
| 任务成功率 | 判定脚本 exit 0 的比例 |
| 首次成功率 | 不经过任何自我纠错轮即为正确的比例 |
| 纠错增益 | 最终成功率 − 首次成功率。**这个数是"agent 会不会自我纠错"的唯一证据** |
| 每任务 token | prompt + completion 合计（分常驻 / 动态两档） |
| 每任务工具轮数 | 平均模型调用轮次 |
| 每任务 wall-clock | 端到端耗时 |
| 危险操作拦截率 | C 类被正确拦截 / C 类总数 |
| 误拦率 | 正常任务被错误拦截的比例 |
| 回滚成功率 | checkpoint 回滚后工作区与提交点完全一致的比例 |
| prompt cache 命中率 | provider 返回的 `cached_tokens / prompt_tokens` |
| 截断发生率 | 触发输出截断的工具调用占比 |

### 7.6 设计主张 → 验证方式 对照表

**这张表就是这份架构方案的自证清单。每一项设计主张都必须有一行支撑。**

| 设计主张 | 验证方式 | 指标 |
| --- | --- | --- |
| 薄脚手架足够用 | ablation：提示词 800 vs 5,000 | 成功率不降，常驻 token 显著降 |
| 工具本身不等于 harness | B1 vs B2 | 成功率差值归因到 harness |
| 会话树比线性省 | 树+压缩 vs 线性全量 | 上下文 token 下降，成功率不降 |
| 常驻区稳定能吃到缓存 | 监控 `cached_tokens` | 命中率 ≥ 一定阈值（实测后定） |
| 工具批次并发有效 | 并发 vs 全顺序 | wall-clock 下降，成功率不变 |
| checkpoint 是有效边界 | 破坏性任务 + 回滚 | 回滚成功率、恢复后成功率 |
| 钩子能减少危险操作 | C 类对抗集 | 拦截率 / 误拦率 |
| 扩展层真的能改变行为 | B2 vs B3 | 扩展引入后目标指标变化 |

---

## 8. 工程契约（CI 门禁）

沿用 interview-agent 已验证的做法，但契约内容不同。

| 门禁 | 内容 | 违反时的表现 |
| --- | --- | --- |
| 分层契约 | `importlinter` 强制 5 层单向依赖，禁止反向 import | CI 失败 |
| 常驻区稳定性 | 单测断言会话内常驻区哈希不变 | CI 失败 |
| 引用缓存契约 | 单测断言 `registry.get()` 在热重载后返回新实例 | CI 失败 |
| **抽象基类契约** | 单测断言：所有 Provider / Tool 实现都继承对应基类；基类含 `@abstractmethod` 则直接实例化必须抛 `TypeError` | CI 失败 |
| **唯一 loop 契约** | 单测断言 `BaseLoop.__subclasses__()` 恰好只有 `AgentLoop` | CI 失败 |
| **消息层不混用** | `importlinter` 断言 `sigma_ai` 不认识 `AgentMessage`；`convert_to_llm` 是 agent→LLM 的唯一引用点 | CI 失败 |
| **摘要降级位置** | 单测断言压缩摘要经 `convert_to_llm` 后 `role == "user"`，不是 `system`（关系 prompt cache） | CI 失败 |
| **未知消息类型** | 单测断言未知类型**不被静默丢弃**（记录 warning 或抛错） | CI 失败 |
| **不透明字段保真** | 单测断言 `*_signature` 类字段经 `convert_to_llm` 后逐字节不变 | CI 失败 |
| 离线可测 | 全部单测在无 API key 下通过 | CI 失败 |
| 依赖锁定 | 精确版本 + lockfile 校验 | CI 失败 |
| 评测回归 | 评测报告与上一次对比，成功率下降超过阈值则报警 | 夜间任务产出报告 |
| 类型检查 | `mypy --strict`（至少覆盖 `core/`） | CI 失败 |

**中间两条是 2026-09-20 拍板 a2（一律继承制）之后新增的。**
原因：`AGENTS.md` 第 2 条是一个**风格要求**，而风格要求默认会随时间腐化——
半年后往代码里塞一个签名匹配的鸭子类型对象，没有任何东西会拦。
把它变成两条可执行的断言，才是让"继承制"真正落地的唯一办法。
这与 4.4 节那条教训同源：**能跑绿的配置不等于生效的约束，约束必须能被单独证伪。**

**依赖方向契约的具体内容**（写在 `pyproject.toml` 的 `[tool.importlinter]`）：

```
sigma         → sigma_tools, sigma_session, sigma_agent, sigma_ai
sigma_tools   → sigma_agent, sigma_ai
sigma_session → sigma_agent, sigma_ai
sigma_agent   → sigma_ai
sigma_ai      → （无内部依赖）
```

同时禁止：任何 `core/` 内的包 import `evals/` 或 `extensions/`。

**注意 `sigma_tools` 与 `sigma_session` 是兄弟层，互不依赖。**
第 3 节那张竖排图只是为了排版，**不是依赖顺序**——照那张图去理解会得出
「sigma_tools 依赖 sigma_session」的错误结论。

这件事必须用**三条契约**才能钉住，只写 `layers` 是不够的：

| 契约 | 类型 | 作用 |
| --- | --- | --- |
| 分层只允许向下依赖 | `layers` | 禁止下层引用上层 |
| 核心层不得依赖评测与扩展 | `forbidden` | 禁止 core 引用 `evals` / `extensions` |
| 内置工具与会话层互不依赖 | `independence` | 钉住兄弟层 |

**为什么 `layers` 表达不了兄弟关系**：它是线性栈，语义只有「下层不许引用上层」，
**默认放行所有向下的 import**。把 `sigma_tools` 排在 `sigma_session` 之上，
就等于给了 `sigma_tools → sigma_session` 一张通行证。

这一点在 2026-09-20 被实测确认过：当时只配了两条契约，向 `sigma_tools`
注入 `import sigma_session`，结果两条契约都是 KEPT、退出码 0 ——
**配置在放行文档明令禁止的依赖**。补上 `independence` 契约后才拦得住，
且双向都能拦。

---

## 9. 分阶段实施计划

**每个阶段都有一个硬性验收门，不达标不进下一阶段。** 这条规则就是针对"骨架到位、实现没接"这个失败模式的直接对策。

| Phase | 内容 | 硬性验收门 |
| --- | --- | --- |
| **P0** 决策与骨架 | 拍板 D1–D6；pyproject；目录；CI；importlinter 契约 | CI 全绿；契约能拦住一个故意写错的反向 import（要有这个测试） |
| **P1** 最小闭环 | `sigma_ai`（**OpenAI 兼容 1 套**）+ `sigma_agent` loop + 4 工具 + 线性会话 + 一次性模式 + FakeProvider | 在真实小仓库上端到端修复一个单文件 bug；agent loop 单测全部走回放、离线通过；**10 条评测任务跑出第一份报告，且含 B1 vs B2 对照** |
| **P2** 会话树与上下文 | SessionTree + `path_to` + 压缩 + `AGENTS.md` + 预算断言 + 输出截断 | 分支 / 回滚可用；常驻区稳定性断言生效；压缩前后成功率下降 ≤ 5% |
| **P3** 钩子与边界 | HookManager + before/afterToolCall + 路径约束 + 影子 git checkpoint + steering/follow-up | 对抗集拦截率 ≥ 90% 且误拦率 ≤ 5%；checkpoint 回滚成功率 100%（含删除文件场景） |
| **P4** 扩展系统 | 扩展加载 + 热重载 + 按名解析契约 + 扩展与内置同路径注册 | 改扩展文件后**当轮生效**，有自动化测试；三个失败场景（导入异常 / 空注册 / 重名）各有单测 |
| **P5** 自证与收尾 | 完整 ablation 矩阵 + 评测报告 + README + 架构文档定稿 + ADR | 7.6 节八项主张**全部**有对照数据；报告可脱离本文件独立阅读 |

**排期提示**：P1 与 P2 各约 3 周，P3 约 2 周，P4 约 3 周，P5 持续。按 8 小时/周，P1–P4 约需一个完整学期。**P1 里就要有评测报告这一条不是可选项**——它是整条路线的地基，越晚做越难补。

---

## 10. 风险清单

按严重程度排序。

### R1. 范围失控（最高风险）

2.3 节砍掉的东西，**一个都不能中途加回来**。判断标准：任何新功能，先问"它对应 7.6 节里哪一条主张"，答不出来就不做。

### R2. 时间被两个项目瓜分

8 小时/周是**总预算，不是每个项目的预算**。如果 sigma 与 interview-agent 并行，实际是 4 + 4。

诚实结论：**4 小时/周做不出本方案的 P1–P4**。所以必须先回答 0.1 节假设 1 里的问题——两者是并行还是替换。这个答案决定的是项目能不能做完，不是排期好不好看。

### R3. 评测集自欺

自己造数据集时，最容易造出"自己的实现恰好能过"的任务。对策：

- B 类任务强制"先失败测试、后任务描述"
- C 类任务在写 agent 之前构造
- 至少 20 条 A 类来自真实仓库而非自造

### R4. Python 热重载的隐性坑

见 4.4 节。三个失败场景必须有单测。若某个场景无法做到确定可控，**就把热重载范围再收窄**，而不是留着不确定行为。可演示但边界清晰 > 能力更强但说不清。

### R5. 工具输出撑爆上下文

见 5.3 节。截断与分页必须同时做。监控"截断发生率"，如果持续走高，说明工具描述或参数设计有问题。

### R6. 演示效应

向他人演示时容易滑向"钩子拦住了危险命令"这种单点演示。**正确的演示方式是给出 7.5 节的指标表**，让数字说话。单点演示经不起"换个写法还拦得住吗"这一问。

---

## 附录 A：与 Pi 的对照速查

| 维度 | Pi | sigma | 差异理由 |
| --- | --- | --- | --- |
| 语言 | TypeScript | Python | 复用既有能力；代价是失去 jiti |
| Provider 协议 | 4 套归一化 | 先 1 套（OpenAI 兼容），共 2 套封顶 | a3 拍板：P1 只要 OpenAI 兼容，已覆盖国内主流 |
| 抽象风格 | TypeScript interface / type | **`abc.ABC` + `@abstractmethod`** | a2 拍板（`AGENTS.md` 第 2 条）：继承制，且忘实现能在实例化时暴露 |
| 内置工具 | 4（+3 可选） | **5**（+3 可选） | 比 Pi 多 `grep`：4 个覆盖不了跨文件搜索定位 |
| 会话存储 | JSONL 树 | JSONL 树 | 照抄 |
| 系统提示词 | < 1,000 token | 800，可调 | 改为用 ablation 定值 |
| 权限 | 无内置，靠容器 | 三层软边界，无容器 | Windows 开发成本 |
| git checkpoint | 使用者自装配 | 核心能力 | 它可量化，可成为自证项 |
| 未知 role 处理 | 静默丢弃（`default → undefined → filter`） | **记录 warning，不静默** | Python 无编译期穷尽性检查，静默会变成「消息莫名消失」（4.0.5 节） |
| 约束采样 | `constrainedSampling`（json_schema / grammar） | **P4 再评估，P1–P3 不做** | 「约束而非校验」价值高，但依赖 provider 侧能力；P1 只有最小闭环，投产比低 |
| 类型安全手段 | `IsJsonCompatible<T>` 编译期体操 | 运行时校验（Pydantic） | 同一诉求的不同手段，效果相当、时机不同 |
| 扩展范围 | harness 全部表面 | 工具 + 钩子 + slash 命令 | 边界清晰才可测 |
| 自扩展 | agent 改自己扩展 + 热重载 | 工具级热重载 | Python 侧正确性风险 |
| TUI | 差分渲染库 | 纯文本 REPL | 零架构信号且拖累 CI |
| 运行模式 | 交互 / print / RPC / SDK | 交互 / 一次性 / SDK | 砍掉产品化接口 |
| 评测 | 社区共享数据集 | 自建 70 条 + ablation 矩阵 | 定位不同：自证 > 规模 |

## 附录 B：待补充的 ADR

P0 阶段为 D1–D6 各写一份 ADR，放在 `docs/decisions/`。格式：背景 / 选项 / 决定 / 后果 / 何时该重新考虑。

**注意签名。** interview-agent 的教训之一是设计文档全部署名为 AI 助手，导致"项目技术深度 > 可自证深度"。每份 ADR 必须写清**你判断的依据和你接受的代价**，这一栏不能由 AI 代填。
