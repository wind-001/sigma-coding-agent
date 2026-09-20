# Pi Agent Harness 研究笔记

> 检索日期：2026-09-20
> 用途：自研 coding agent 的架构与理念参考
> 状态：外部资料交叉验证完成，已标注可信度分级（见第 15 节）

---

## 0. 结论先行

1. **Pi 最值得抄的不是功能，是它的切分线。** 它把"机制"留在核心里，把"策略"全部推出去。核心只回答"一轮 agent 循环怎么跑"，不回答"你应该怎么工作"。这条线的位置，比它具体实现了什么更重要。

2. **它的核心命题是有条件成立的，别当公理抄。** "瘦脚手架 + 强模型 > 厚脚手架"成立的前提是：模型自己已经知道怎么写代码（RL 训练过），且任务在单文件/单仓库规模内。中文二手资料把这个前提抹掉了，读起来像是"功能少所以更强"，这是误读。

3. **真正的差异化是运行时自扩展，而它恰好是最难抄的部分。** 扩展文件被 agent 自己改写后热重载、当轮即生效——这要求扩展层是一个**运行时可变的状态**，而不是编译期的插件注册表。多数自研 harness 在这一步会把扩展做成启动时装配，那就永远得不到这个能力。

4. **对星辰的定位：这不是一个"拿来用的产品"，是一个"拿来读的参考实现"。** 它小到能端到端读完（这本身就是设计目标），且分成了可以单独使用的层。做自研 harness 时，正确的用法是挑其中一层的思想，而不是整体照搬。

5. **最大的现实陷阱：版本、包名、star 数在中文资料里几乎全是错的。** 项目 2026-04 被收购后仓库和 npm scope 都迁移过，大量教程仍指向旧包（已 deprecated）。照抄搜索结果里的安装命令会装错包。见第 1 节。

---

## 1. 项目基本信息（先纠正资料里的错误）

| 项 | 值 | 备注 |
| --- | --- | --- |
| 官方仓库 | `earendil-works/pi` | 旧地址 `badlogic/pi-mono` 会重定向 |
| 作者 | Mario Zechner（libGDX 作者，@badlogic） | 2026-04 由 Earendil 收购并接管 |
| 许可证 | MIT | |
| 主语言 | TypeScript monorepo | |
| 当前 CLI 包 | `@earendil-works/pi-coding-agent` | 正确安装命令见下 |
| 已废弃包 | `@mariozechner/pi-coding-agent@0.73.1` | 标记 deprecated，不要用 |
| 同名陷阱包 | `pi-coding-agent`（无 scope） | **不是官方包** |
| Node 要求 | >= 22.19.0 | |
| 官网 | pi.dev | |

正确安装命令：

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
```

**可信度说明**：包名迁移这件事有两处独立来源印证（pi.dev 官方页 + 一篇 2026-08 的实务指南），可信。但**版本号和 star 数在不同来源之间严重不一致**（同一时期被分别描述为 5 万 / 6.9 万 / 9.2 万 star，版本从 0.80.3 到 0.84.1 都有），这是不同时间点的快照混在一起。**不要引用任何具体数字作为论据。**

---

## 2. 设计理念：三个核心赌注

### 赌注一：Agent = Model + Harness

这是它全部设计的出发点。Harness 不是模型外面一层"包装"，而是规定了 agent 的：

- 能力边界（有哪些工具）
- 行为边界（允许做什么、什么时候停）
- 状态边界（历史怎么存、怎么压缩）
- 权限边界（能碰什么、以什么身份碰）
- 演化边界（能不能改自己）

推论：**讨论 agent 能否扩展、自生长，本质上讨论的不是模型参数，而是 harness 能否安全地改变自身结构。** 这句话是整个方向的立论基础。

### 赌注二：原语优先（primitives, not features）

不是"内置所有功能"，而是：

```
提供少量原语 + 开放扩展接口 + 允许用户自行组合
```

官方 slogan 是 "There are many agent harnesses — but this one is yours"。它对称的立场是：**重型工具的功能膨胀本身在损害上下文管理和可读性**，所以不做。

### 赌注三：上下文窗口是真正的瓶颈

这是它砍功能的理由，也是最容易被误读成"偷懒"的地方。它的论证是：

- 系统提示词本身不该编码模型已经知道的行为 → 因此压到 1000 token 以下（有时约 200）
- MCP 服务器实测可往上下文里塞 13,700+ token → 因此默认不接 MCP
- 技能内容不该在每轮都注入 → 因此用渐进披露（progressive disclosure）

**作者的前提假设**（2025-11 博客）：RL 训练过的前沿模型，推理能力已经足够，薄脚手架在编码任务上会跑赢厚脚手架。**注意这是个条件命题**——换成弱模型或高度非标准的代码库，这个结论不成立。

---

## 3. 刻意不做的事（及替代路径）

这张表是 Pi 的设计意图最集中的体现。**每个"不做"都对应一条替代路径，而不是一个空缺。**

| 不做 | 官方立场 | 替代路径 |
| --- | --- | --- |
| MCP | 不内置；CLI 工具用 README 描述即可 | 写扩展实现，或装社区包 |
| 子代理 | 不内置 | 扩展里启动新的 Pi 实例 |
| 权限弹窗 | 不内置，偏好容器隔离 | 拦截 `tool_call` 事件做审批流 |
| Plan Mode | 不内置 | 计划写进文件，或装实现它的包 |
| 待办追踪 | 不内置 | 文件，或自定义扩展 |
| 后台 bash | 不内置 | 推荐 `tmux` 以获得可观测性 |

另外可显式限制工具暴露，用于只读审查等场景：

```bash
pi --tools read,grep,find,ls -p "审查这个仓库的权限边界和数据一致性风险"
pi --exclude-tools bash
pi --no-builtin-tools -e ./my-tools.ts
```

**必须区分的两个概念**（资料里常被混淆）：工具 allowlist 只决定"哪些工具暴露给模型"，**不等于操作系统沙箱**。只要 harness 或某个扩展还能执行本地代码，它就拥有启动 Pi 的那个用户的权限。

---

## 4. 架构：分层包结构

**注意：不同来源给出的包数量不同（4 包 / 6 包 / 10 包），因为快照时间点不同。** 下面取信息最新、且与官方 `earendil-works` scope 一致的一组，按依赖层级自底向上：

| 包 | 职责 |
| --- | --- |
| `pi-ai` | 统一多 Provider 的消息、工具调用、推理、图片、用量与成本接口 |
| `pi-agent-core` | Agent 循环、状态机、工具执行、事件流、消息队列 |
| `pi-tui` | 终端界面、Markdown、Diff、图片、差分渲染（约 600 行） |
| `pi-telemetry` | 不绑定供应商的中立遥测契约与类型 |
| `pi-coding-agent` | 组装成 CLI，加入会话、上下文、压缩、资源发现、默认编码工具 |

此外仓库里还有 `pi-protocol` / `pi-client` / `pi-server` / SQLite session backend，但**远程 session 能力仍是实验性的**，不等于成熟稳定的远程服务。当前 CLI 默认会话仍是 JSONL 文件，不是 SQLite。

**设计要点**：每一层都能单独使用。想研究 agent 运行时的人可以只拿 `pi-ai` 或 `pi-agent-core`；想要成品 CLI 的人直接用完整的。这个"分层 + 可单独消费"是它同时能当产品又能当 toolkit 的原因。

**四种运行模式**：

| 模式 | 用途 |
| --- | --- |
| Interactive | 完整 TUI |
| Print / JSON | `pi -p "query"` 用于脚本；`--mode json` 输出事件流 |
| RPC | stdin/stdout JSON 协议，供非 Node 集成 |
| SDK | 嵌入自己的应用 |

SDK 入口形态（用于参考 API 设计）：

```typescript
const { session } = await createAgentSession({
  sessionManager: SessionManager.inMemory(),
  authStorage,
  modelRegistry,
});
```

---

## 5. Agent Loop：一轮到底发生什么

这是最值得精读的部分（对应 `agent-loop.ts`）。流程：

```
接收 AgentMessage[]
   ↓
transformContext()   —— 可重塑已保存的上下文
   ↓
convertToLlm()       —— 转成该 provider 能接受的消息格式
   ↓
模型流式返回文本 + 工具调用
   ↓
校验工具参数
   ↓
执行完整的工具批次（batch）
   ↓
逐个 append toolResult
   ↓
判断是否需要再来一轮模型调用
```

**关键设计判断**：核心只重复"这一轮的机制"，**不做工作流裁决**。规划、审批、委派这些策略全部在循环之外。

暴露给外部干预的钩子：

- `beforeToolCall` / `afterToolCall` —— 可以阻断工具调用，或改写工具返回结果
- `transformContext` / `convertToLlm`
- steering message、follow-up 队列、abort signal、`shouldStopAfterTurn`

**工具执行是批次化的**：模型一次请求的整批工具会被完整执行，而不是逐个执行后重新问模型。并发/顺序可按工具声明控制。

**steering 机制（人机协同的关键）**——agent 在跑的时候人不用干等：

| 操作 | 行为 |
| --- | --- |
| `Enter` | 提交 steering message：当前 assistant turn 完成工具调用后送达，**会打断剩余未执行的工具** |
| `Alt+Enter` | 提交 follow-up：等 agent 自己认为任务完成后才送达 |
| `Escape` | 中止，并把队列中的消息恢复到编辑器 |

实际价值：可以在执行途中说"别继续改数据库层，只修 API 适配器"，而不必等它把错误方向走完。

---

## 6. 会话模型：为什么是一棵树

**这是最容易被低估、但最值得抄的一个设计。**

多数 coding agent 把对话存成线性记录。Pi 存成 **JSONL 树**：除 header 外，每条记录都有 `id` 和 `parentId`。因此**一个会话文件里可以容纳多个历史分支**。

默认位置：`~/.pi/agent/sessions/`，按工作目录组织。

| 操作 | 语义 |
| --- | --- |
| `/tree` | 原地导航会话树，可以跳到任意历史点继续——**不产生新文件** |
| Fork | 从某条早先的用户消息复制出新会话 |
| Clone | 把当前分支复制成新会话文件 |
| `/branch` | 通过消息选择器分叉成新会话文件 |
| Compaction | 接近上下文上限时自动/手动摘要旧消息 |

树的可用性设计：支持按消息类型过滤（默认 / 无工具 / 仅用户 / 已标记）、条目加书签、切分支时可选生成被放弃分支的摘要。

**压缩（compaction）**的取舍说得很清楚：`/compact` 对**实时 prompt 是有损的**，但**磁盘上的完整历史仍然保留**。且压缩策略可通过扩展完全自定义——可以做成按主题压缩、代码感知摘要、或换更便宜的模型来摘要。

---

## 7. 扩展系统与自扩展

### 7.1 扩展是什么

TypeScript 模块，通过 `jiti` 加载——**无构建步骤**。它能做的事覆盖整个 harness 表面：

- 注册新工具、slash 命令、键盘快捷键
- 处理 20+ 生命周期钩子（session / turn / tool / context）
- 在每一轮之前注入消息（前馈上下文）
- 过滤消息历史（上下文管理）
- 实现 RAG 或自定义检索
- 构建长期记忆
- 实现子代理、权限门、SSH 执行、sandbox、MCP 集成、自定义编辑器、状态栏、浮层

**结论**：别的工具内置的任何能力，原则上都能被复现成一个 Pi 扩展。

### 7.2 自扩展（真正的杀手锏）

流程是这样的：

```
用户对 Pi 说："加一个 run_tests 工具"
   ↓
Pi 自己写一个 TS 扩展文件
   ↓
jiti 热重载
   ↓
registerTool 注册第 5 个工具
   ↓
Pi 在同一个 run 里开始用它
```

**全程不重启会话。** 这是它和"插件系统"的本质区别：插件的加载时机是启动时，而 Pi 的扩展层是**运行时可变状态**。

扩展 API 形态（用于参考接口设计）：

```typescript
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "run_tests",
    label: "Run tests",
    description: "Run the project's test suite and return the summary.",
    parameters: Type.Object({
      path: Type.String({ description: "File or directory to test" }),
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      onUpdate?.({ text: `Running tests in ${params.path}...` });
      // ...
      return {
        content: [{ type: "text", text: stdout || stderr }],
        details: { path: params.path },
      };
    },
  });

  pi.on("session:start", () => {
    pi.log?.("run_tests extension loaded");
  });
}
```

注意几个接口设计点：参数用 `typebox` schema 声明；`execute` 签名里带 `signal`（取消）和 `onUpdate`（流式进度回调）；返回值固定为 `{ content: [...], details: {...} }` 两段式——**给模型看的内容和给程序看的结构化数据分开**。

### 7.3 生态规模

社区扩展目录在 2026 年初已有 2000+ 包。看一个具体例子（`@ruizrica/agent-pi` 包）能理解这套扩展模型的上限在哪：

- Multi-Agent Orchestration：team（dispatch 型编排）/ chain（顺序流水线，`$INPUT` 传递）/ pipeline（多阶段 + 并行派发）
- Security：`tool_call` 前置钩子拦 `rm -rf` / `sudo` / 凭证窃取 / prompt injection
- Viewers：浏览器 GUI 做计划审批，带 checkbox、重排、行内编辑
- Session & Context：`memory-cycle` 做"记忆感知的压缩"——在压缩前保存、压缩后恢复上下文

主题、Prompt Templates（`/name` 展开，支持 `{{ variable }}` 插值）、Skills 都可以打包成 Pi Package，通过 npm 或 git 分发：

```bash
pi install npm:@foo/pi-tools
pi install git:github.com/user/pi-package#v1.2.0
```

---

## 8. Context Engineering

Pi 在这一层的立场很明确：**"最小系统提示词 + 可扩展性"这两个特性合起来，才让真正的上下文工程成为可能**——因为你控制什么进上下文、以及上下文怎么被管理。

| 机制 | 说明 |
| --- | --- |
| `AGENTS.md` | 项目指令，启动时从 `~/.pi/agent/`、各级父目录、当前目录加载 |
| `SYSTEM.md` | 按项目**替换或追加**默认系统提示词 |
| Compaction | 接近上限时自动摘要；策略可通过扩展完全自定义 |
| Skills | 能力包，**按需加载**——渐进披露，且不破坏 prompt cache |
| Prompt Templates | Markdown 文件，`/name` 展开 |
| 动态上下文 | 扩展可在每轮前注入消息、过滤历史、做 RAG、建长期记忆 |

配置分两层：`~/.pi/agent/settings.json`（全局）与 `.pi/settings.json`（项目级，merge）。

**"渐进披露不破坏 prompt cache"这一点值得单独拎出来**：如果技能内容每轮都注入同一段文本，那是稳定的、可缓存的；如果按需动态插入，插入位置和时机就会影响缓存命中。Pi 声称的"按需加载但不爆缓存"是一个具体工程权衡，自研时要实测，不要默认成立。

---

## 9. 安全模型

### 9.1 默认姿态

**Pi 默认是宽权限的**：启动时弹一次信任提示（信任项目 / 父目录 / 仅本次会话 / 拒绝），一旦信任，agent 就能用完整工具读写执行。**没有内置权限弹窗**——这是刻意的，因为它认为审批这种事该由使用者按自己的风险模型来建。

### 9.2 硬边界：容器隔离

官方推荐用 Docker 之类的容器作为硬隔离边界（host FS 不暴露）。社区/二手资料提到的三种模式（可信度较低，见第 15 节）：micro-VM 硬件隔离、Plain Docker 完整容器化、seccomp 系统调用白名单。

### 9.3 软边界

- 装带 guardrail 的包（命令/路径策略）
- 自定义钩子，阻断 `sudo`、`rm -rf`、写 `.env`
- 用 `--tools` 起一个缩减后的工具集

### 9.4 供应链安全（这部分被严重低估）

这是它在开源 agent 工具里少见的做法：

- 所有外部依赖**精确版本锁定**
- lockfile 校验 + 受控的生命周期脚本
- 生成 shrinkwrap 以复现安装
- CI 中强制 `npm ci --ignore-scripts` + 安全审计
- pre-commit 拦截 lockfile 漂移

它自己的安装命令也带 `--ignore-scripts`。**这个纪律本身比任何一个功能都更值得抄**——一个能读写代码库、还能执行 shell 的 agent，供应链被投毒的影响面是灾难级的。

---

## 10. 多 Provider 抽象

`pi-ai` 的工作是把四套 wire protocol 归一化：OpenAI Completions、OpenAI Responses、Anthropic Messages、Google Generative AI。

| 维度 | 做法 |
| --- | --- |
| Provider 范围 | 15+：Anthropic / OpenAI / Azure / Google Gemini / Vertex / Bedrock / DeepSeek / Mistral / Groq / Cerebras / xAI / OpenRouter / Vercel AI Gateway / Cloudflare / HuggingFace / Fireworks / Together / Baseten / Kimi / MiniMax / ZAI / Qwen / Xiaomi MiMo / Ollama / LM Studio / vLLM |
| 认证 | API key 或 OAuth；`~/.pi/agent/auth.json` 优先于环境变量 |
| 切换 | `/model` 或 `Ctrl+L` 中途切换；`Ctrl+P` 循环收藏；`Shift+Tab` 切推理强度 |
| 自定义 | `~/.pi/agent/models.json` 注册兼容协议的本地/内部服务，打开 `/model` 时热重载 |
| 成本 | 内建 usage 与成本追踪 |

**两个容易误解的边界**：

1. **"接口兼容"不等于"工具调用可靠"**——本地模型接进来之后，function calling 的稳定性和失败恢复仍需独立验证。
2. **订阅制登录不等于免费**——官方文档说明第三方 harness 用 Claude Pro/Max 认证走的是 extra usage、按 token 计费，不消耗套餐内额度。

---

## 11. 工程纪律（最容易被忽略、但最可抄）

- 依赖变更视为与代码审查同等重要
- 精确版本锁定；workspace 内联包保留语义化版本范围
- 分级测试脚本：`./test.sh` 在无 API key 时自动跳过模型集成测试，**离线也能验证核心逻辑**；`./pi-test.sh` 支持从源码直接跑调试会话
- 数据共享：鼓励提交脱敏后的真实任务轨迹（JSONL，含工具调用链、失败案例、修复路径）到 `pi-share-hf` 数据集；本地哈希化 + 差分隐私噪声，上传前正则清洗代码片段

**"无 API key 也能跑核心测试"这一条，是自研 agent 项目最该优先实现的工程能力**——否则 CI 永远跑不起来，回归测试就永远不存在。

---

## 12. 关键数字汇总（引用前请先看可信度）

| 数字 | 含义 | 可信度 |
| --- | --- | --- |
| < 1,000 token | 系统提示词体量（有时约 200） | 高，多处一致 |
| 4 | 默认内置工具（read / write / edit / bash） | 高，多处一致 |
| 7 | 完整内置工具集（+ grep / find / ls，可选） | 高 |
| 13,700+ token | 作者举例的 MCP 服务器上下文开销 | 中，单一来源 |
| 20+ | 扩展生命周期钩子数 | 中 |
| 2,000+ | 社区扩展包数量（2026 年初） | 中 |
| 82 | Terminal-Bench 任务数；Pi 排第 2（配 Claude Opus） | **低，需自行验证** |
| ~600 行 | `pi-tui` 规模 | 中 |
| ~55,000 token | 对比表里 Claude Code 的系统提示词体量 | **低，数字可疑，勿引用** |

**建议**：如果要在作品集/汇报里引用 benchmark，**自己去跑一遍**。二手 benchmark 数字是面试里最容易被戳穿的东西。

---

## 13. 上位框架：Harness Engineering

Pi 是"一个具体实现"。要理解它为什么这么切分，需要知道它所属的理论框架（来源：Birgitta Böckeler / Thoughtworks，2026-04，把控制论引入 harness 设计）。

### 13.1 Agent = Model + Harness

```
Agent = Model + Tool Harness + User Harness
```

- **Tool Harness**：工具与编排，由产品方提供（Claude Code、Cursor 这类）
- **User Harness**：项目特定的规则、测试、上下文文档，**由开发者自己塑造**

关键论断：**赢的不是最强的模型，是装备最好的那个**。好模型在差环境里，会稳定输给中等模型在好环境里。

### 13.2 两类控制：Guides 与 Sensors

| 类型 | 方向 | 目的 | 例子 |
| --- | --- | --- | --- |
| **Guides**（前馈） | agent 行动**之前** | 预判不想要的输出，提前引导，提高一次做对的概率 | AGENTS.md、skills、参考文档、编码规范、bootstrap 脚本 |
| **Sensors**（反馈） | agent 行动**之后** | 观察结果，支持自我纠正 | linter、类型检查、测试、AI 代码审查、浏览器自动化 |

**单独用任何一个都会坏掉**：

- 只用 feedback → 得到一个不断重复同样错误的 agent
- 只用 feedforward → 得到一个编码了规则、却永远不知道规则是否奏效的 agent

### 13.3 计算型 vs 推断型

| 类型 | 执行 | 速度 | 可靠性 | 例子 |
| --- | --- | --- | --- | --- |
| Computational | CPU，确定性 | 毫秒~秒 | 可靠一致 | 测试、linter、类型检查器 |
| Inferential | GPU/NPU，LLM | 慢、贵 | 非确定 | AI 代码审查、LLM-as-a-judge |

计算型传感器便宜到可以每次变更都跑，和 agent 并行。推断型只在集成后流水线或高价值检查上用。

**一个很强的模式**：让**推断型传感器的输出为 LLM 消费优化**——比如自定义 linter 消息不仅报告错误，还带上自我修正指令。这本质上是**正向的 prompt injection**，直接喂进 agent 的自我纠正循环。

### 13.4 Steering Loop（人的角色被重新定义）

人不再逐轮盯梢，而是**通过迭代 harness 本身来 steer agent**。当某个问题反复出现，正确的反应是**改进前馈或反馈控制**，而不是盯得更紧。

反过来说，agent 可以参与构建自己的 harness：写结构化测试、从观察到的模式生成规则草稿、搭自定义 linter、从代码库考古出 how-to 指南。

### 13.5 质量控制点分布（Keep Quality Left）

| 时机 | 控制 |
| --- | --- |
| commit 前 / agent 运行中 | 快速 linter、类型检查、基础代码审查 agent、前馈 guides |
| 集成后流水线 | 重复所有快速控制 + 昂贵的：变异测试、架构审查、深度推断型审查 |
| 持续 / 生命周期之外 | 漂移检测（死代码、覆盖率质量、依赖扫描）、运行时反馈（SLO、响应质量采样、日志异常） |

### 13.6 三个调节维度

| 类别 | 管什么 | 成熟度 |
| --- | --- | --- |
| **Maintainability Harness** | 内部代码质量：重复、复杂度、覆盖率、风格、架构漂移 | 最成熟，已有工具直接映射 |
| **Architecture Fitness Harness** | 用 fitness function 定义并强制架构特性；前馈定约定，反馈跑性能测试验证有没有劣化 | 中 |
| **Behaviour Harness** | 应用是否真的按预期工作 | 最难；目前依赖前馈功能规格 + AI 生成的测试套件，但只靠 AI 生成测试不够 |

**两种控制的失效边界要说清楚**：计算型 + 推断型都**无法可靠捕获**"需求被误诊""功能做多了""指令被误解"这类高影响问题——这些仍需人的判断。

### 13.7 Harnessability（顺带一个重要概念）

不是所有代码库都同样"可被 harness"。**Ambient affordances** 指环境让 agent 可读、可控的结构性质：

- 强类型语言天然提供类型检查传感器
- 现代框架抽象掉了可能让 agent 困惑的复杂度
- 绿地项目可以在第一天就把 harnessability 编进架构

**遗留系统的悖论**：最需要 harness 的地方，恰恰是最难建 harness 的地方。

---

## 14. 对自研 coding agent 的可操作结论

### 14.1 值得直接继承的

1. **机制与策略分离**。核心循环只做"流式响应 → 执行工具批次 → 追加结果 → 判断是否继续"。规划、审批、委派、todo 一律外置。这条线画对了，后面所有扩展都不用改核心。
2. **会话存成树（`id` + `parentId`）而不是线性数组**。成本极低，收益很大：分支、回滚、多方案对比全都免费获得。**这是本次调研中性价比最高的一个具体设计。**
3. **`beforeToolCall` / `afterToolCall` 钩子**。有了这两个，审批、路径保护、结果改写、审计日志全都能从外部实现，核心不用感知。
4. **工具返回值分两段：给模型看的 `content` 和给程序看的 `details`**。
5. **工具参数用 schema 声明（typebox / JSON Schema）**，并让 `execute` 带 `signal` 和 `onUpdate`——取消和流式进度必须在一开始就进接口，后加是破坏性变更。
6. **steering / follow-up 双队列**。`Enter` 打断剩余工具、`Alt+Enter` 等任务完成。这是"人在回路"最实用的形态。
7. **无 API key 也能跑核心测试**。这条决定了项目有没有回归测试，进而决定它能不能长期演进。
8. **供应链纪律 + `--ignore-scripts`**。一个能执行 shell 的 agent，投毒影响面是灾难级。

### 14.2 值得改造的

| Pi 的做法 | 改造建议 | 理由 |
| --- | --- | --- |
| 系统提示词压到 <1000 token | **不要一次砍到底**。先量基线，再逐段删并跑评测 | 它的前提是"前沿模型已 RL 训练过编码任务"。用非前沿模型或领域特殊场景时，这个前提不成立 |
| 无权限弹窗 | 至少保留一个最小审批通道（危险命令白名单/黑名单） | 它的替代方案是容器隔离，那是运维成本。个人项目里，一个 `tool_call` 前置钩子更现实 |
| 扩展靠 TypeScript + jiti 热重载 | 看你的技术栈。**热重载是能力，不是实现细节** | 核心要求是"扩展层是运行时可变状态"。做不到这点，就别声称有自扩展 |
| 默认不接 MCP | 建议至少支持，但**工具要按需暴露** | Pi 反对的是"无条件塞进上下文"，不是 MCP 本身。这个区分在面试里能加分 |
| JSONL 存会话 | 保留 JSONL 的追加写特性，但**考虑同时建索引** | 纯 JSONL 做检索和统计会很痛苦 |

### 14.3 不要抄的

1. **不要抄"功能少所以更强"这个叙事。** 它的真实论证是"上下文窗口是瓶颈 → 因此减少常驻 token 开销"。把因果关系说反了，就变成了给偷懒找理由。
2. **不要抄它上层的产品取舍**（不做 plan mode / 不做子代理），那些是它的定位选择，不是技术结论。**你的 agent 需要什么由你的使用场景决定。**
3. **不要抄单文件 `agent-loop.ts` 的规模。** 它的极简能成立，是因为下游有 2000+ 社区扩展补足。你没有那个生态。
4. **不要照抄任何二手资料里的版本号、star 数、benchmark 排名。** 见第 15 节。

### 14.4 开工前需要拍板的决策点

这五个问题如果有明确答案，架构基本就定了：

1. **harness 的边界画在哪？** 核心负责到哪一层——只到 agent loop，还是到会话管理，还是到 CLI？
2. **扩展层是运行时状态还是编译期注册表？** 这决定了"自扩展"是不是真能力。
3. **上下文预算怎么管？** 常驻（系统提示 + 工具定义 + AGENTS.md）占多少 token，动态部分怎么按需加载而不爆 prompt cache？
4. **安全边界是容器还是钩子？** 容器是硬边界但成本高；钩子是软边界但必须自己穷举危险面。
5. **可验证性怎么设计？** 用什么数据集、什么 baseline、什么 ablation 来证明你的 harness 设计确实有效。**没有这一步，最后又只能靠"我觉得这样更好"来交差。**

---

## 15. 来源与可信度分级

### 高（官方或官方直接转载）

- pi.dev 官方站点（产品主张、功能清单、四种模式、steering、AGENTS.md/SYSTEM.md/compaction/skills 机制）
- `earendil-works/pi` 仓库文档（`agent-loop.ts`、`docs/rpc.md`、扩展 API、事件钩子）
- 2026-08 实务指南（包名迁移、settings 两层、CLI flags、会话命令，与官方描述自洽）

### 中（第三方但详实、内部自洽）

- agentic-ai.readthedocs.io 的 Pi 词条（包结构、刻意省略表、对比表）
- agentwiki 的 Pi 词条（JSONL DAG、扩展钩子数、设计哲学、横向对比）
- superteams.ai 词汇表（扩展 API 代码示例、Terminal-Bench 结果）
- 源码级对比文章（`agent-loop.ts` 的 `transformContext` / `convertToLlm` / `beforeToolCall` / `afterToolCall`，明确给出对比的三个 commit hash——方法论最严谨的一篇）
- Harness Engineering 理论框架（Thoughtworks / Böckeler，控制论框架）

### 低（存在明显错误或未印证的说法，仅作线索）

- **某中文技术博客（含"微 VM / unikernel / Gondolin / OpenShell"那篇）**：开篇称项目由"Sapiens AI 推出"（与官方不符）、称按任务类型自动路由到 CodeLlama/Claude 3.5（在官方资料中无任何印证）、容器隔离三模式的命名与细节均无官方来源。**三条硬伤，整体不建议引用。**
- pi.dev 上第三方包页面里的扩展清单（描述的是某个具体社区包，不是核心能力）
- 各来源中的 star 数、版本号（时间点混杂，互相矛盾）
- "Terminal-Bench 排名第 2"（仅单一来源，且未给出口径与日期）
- 对比表中 "Claude Code 系统提示词 ~55,000 token"（数量级存疑）

### 交叉印证结论

**架构分层、四个工具、系统提示词体量、刻意省略清单、会话树模型、扩展机制、steering 双队列——这七项在三处以上独立来源中一致，可以直接采信。**

**所有性能与生态规模的数字，只当方向性参考，不进任何正式文档。**
