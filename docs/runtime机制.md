# runtime 机制索引

> 建立：2026-10-06。本文回答两个问题：**跑一轮任务时哪些机制真的会触发**（主表），
> 以及**这些东西分别住在哪个文件里**（附表）。
>
> 口径警告（先读这段再看表）：**"组件"在本项目不是一个正式单位**。本项目的正式单位是
> 「功能域子包」（15 个，目录单位，`lint-imports` 强制）与「机制」（行为单位，可单独关断）。
> 本文用的是后者。词汇定义见根 `CONTEXT.md`。
>
> **为什么 runtime 机制不能按 `src/sigma/runtime/` 这个目录来数**：那个包只有 4 个文件，
> 而 `AgentLoop` 一个类里就装了轮循环、流式聚合、批次执行、写批次快照、steering、
> 信箱收尾、重试退避七个机制；压缩、沙箱、审批、持久化则完全住在别的包里。
> 按包画会得到"几个类"，按机制画才是"这套东西怎么跑起来的"。

---

## 主表：机制 × 触发时机

**时机**列是 `send(task)` 一次调用的时间轴顺序；**关闭方式**列是实证可单独关掉的入口
（空 = 没有开关，改动即改行为，需靠注入实验证伪）。行号对应当前 HEAD，会随代码漂移。

| # | 机制 | 触发时机 | 落点（文件:行） | 关闭方式 |
| --- | --- | --- | --- | --- |
| 1 | **常驻区指纹闸** | 每次组装消息（`build_messages`） | `sessions/context.py:247` `verify_resident_region` | 无（违约即抛） |
| 2 | **常驻区预算闸** | 同上 | `sessions/context.py` `verify_resident_budget`；表在 `config/resident_caps.py` | 无（违约即抛） |
| 3 | **懒基线** | 首个写批次前（构造期**不打** baseline） | `security/shadow_checkpoint.py`（G65 语义） | `--no-checkpoint` |
| 4 | **扫描期冻结** | 会话构造时扫一次，会话内不再重扫 | `sdk.py:468-494`（技能/记忆/repo map 三样同款） | `--no-memory` · `--no-repo-map` · `--skills-dir` |
| 5 | **阈值压缩** | `send()` 里、任务追加进历史**之前** | `sdk.py:1135` `_compact_if_needed` → `sessions/compaction.py` `should_compact` | `enable_compaction=False`（B1 档） |
| 6 | **悬空 tool_call 修复** | 每轮前 + 接续时 | `sessions/repair.py` `repair_dangling_tool_results`；`sdk.py:1065` | `repair_dangling=False` |
| 7 | **打断检查点** | 每轮开头 | `runtime/event_loop.py:341` `raise_if_cancelled` | 无（`NeverCancelled` 仅评测/子 agent） |
| 8 | **todo steering** | 每轮开头（连续 N 轮没碰 todo 时） | `event_loop.py:344` `_todo_steer_if_due` | `--no-todo` / `todo_steer_interval=0` |
| 9 | **用户 steering 注入** | 每轮开头、模型调用前 | `event_loop.py:352` `_drain_steering` | 无（`!text` 是唯一入口） |
| 10 | **信箱注入** | 每轮开头（子任务结果） | `event_loop.py:354` `_drain_mailbox` | `--no-sub-agent` |
| 11 | **上下文降级** | 每轮（agent 层 → LLM 层） | `event_loop.py:358` `_to_llm` → `agent/messages.py` `convert_to_llm` | 无（唯一通道，import-linter 钉住） |
| 12 | **流式聚合** | 每轮模型调用 | `event_loop.py:633` `_stream_model`；分片归 `providers/tool_calls.py` | 无 |
| 13 | **瞬时故障指数退避** | 流零产出且全 TRANSIENT 时（最多 3 次） | `event_loop.py:136` `TransientBackoff`；重试块 `:750-762` | 构造参数 `transient_backoff` / `backoff_sleep` |
| 14 | **错误透传（不假装成功）** | 每轮 LLM 返回后、工具批次**之前** | `event_loop.py:387` → `TurnResult(status="error")` | 无（2026-09-24 review 修复的那条） |
| 15 | **硬熔断** | 每轮末（纯文本轮也过闸） | `runtime/loop_guard.py:229-247`：连续错误 5 · 同签名失败 3 · token 200 万 · 墙钟 30 分 · 绝对轮数 200 | `GuardConfig` 各字段置 `None` |
| 16 | **软检测停滞 nudge→stop** | 每轮末（窗口 6 轮、至少 3 轮有动作） | `loop_guard.py:260`；nudge 注入 `event_loop.py:517` | `GuardConfig.disabled()`（子 agent 用） |
| 17 | **审批等待不计墙钟** | 审批询问前后 | `event_loop.py:944-951` `guard.pause()/resume()` | 无（评审必修 5） |
| 18 | **L3 审批拦截** | 批次内、逐个调用、执行前 | `event_loop.py:938-974`；规则 `security/approval.py:56` `DANGEROUS_PATTERNS` | `--no-approval` |
| 19 | **L3 越界豁免** | 批准时随决策回传 | `event_loop.py:972` `outside_flags` → `ToolContext.outside_approved` | — |
| 20 | **只读并发 / 写批次串行** | 批次内 | `event_loop.py:976-1010` | 无（确定性回放的地基） |
| 21 | **tool_lock 写互斥** | 写批次 + checkpoint.mark 一并罩住 | `event_loop.py:1007`；主/子 agent 共用一把 | `enable_sub_agent=False` 时无锁 |
| 22 | **L2 影子快照 mark** | 写批次**执行前**；失败静默降级 | `event_loop.py:1066` `_mark_before_writes` → `security/shadow_checkpoint.py` | `--no-checkpoint` |
| 23 | **L1 写路径沙箱** | 每个写工具的路径解析 | `security/path_sandbox.py:64` `resolve_path`（`read` 不限制） | 无 |
| 24 | **工具输出截断** | 每个工具返回时 | `tools/truncate.py`（8 KB / 头 40 行 / 尾 40 行） | 无 |
| 25 | **持久化（逐事件写穿）** | 三个时机：LLM 返回 · 工具结果落定 · 注入 | `hooks/persist.py` `SessionPersistHook` | 无（正确性要求） |
| 26 | **trace 采集** | 全程（与会话文件同目录、同名不同后缀） | `observability/trace.py` `TraceHook` | `--no-obs`（子 agent 恒关） |
| 27 | **收尾信箱兜底** | 模型不再调工具后 | `event_loop.py:429` `_drain_or_wait_mailbox` | 无（没有它子任务结果永远留在信箱里） |
| 28 | **溢出恢复** | `ProviderError(CONTEXT_OVERFLOW)` 或 error 结果 | `sdk.py:1044` `_attempt_turn`：修悬空 → 强压 → 重跑，**只恢复一次** | `enable_compaction=False` |
| 29 | **会话接续 / 切换** | REPL 换会话时 | `cli/main.py` `SessionManager`；`sdk.py` `tree=` 参数 | 不传 `tree` = 新会话 |
| 30 | **扩展热重载（当轮生效）** | `/reload` | `sdk.py:895` `reload_tools` → `tools/registry.py:256` 五步装载 | `--no-extensions` |
| 31 | **联网档位热替换** | 工作台开关 | `sdk.py:916` `set_web_tools`（**必须重建 registry+context+loop 三样**） | 默认关 |
| 32 | **子 agent 派发** | 模型调 `task`（后台，最多 3 并发） | `runtime/sub_agent.py` `TaskTool`；轮数三档 10/20/30 | `--no-sub-agent` |
| 33 | **团队引擎** | `multi_agent` 的 start | `team/engine.py` `TeamEngine.run`：常驻 worker + Scanner 轮询 + 自动收敛 | `--no-team` |
| 34 | **lease 心跳续租** | worker 存活期间每 TTL/3 | `team/engine.py` `_heartbeat_loop`（LLM 不可见） | `--no-team` |
| 35 | **角色物理分权** | 子会话构造时换板面 | `sdk.py:717-731`：`clone(exclude=...)` 后按 role 重注册 `LeadBoard`/`WorkerBoard` | `--no-team` |
| 36 | **失败必达 lead** | 任务 fail 时（去重键 = 任务 id + attempts） | `team/engine.py` `_notify_failures` | `--no-team` |
| 37 | **IO 故障不许杀死 worker** | 引擎 IO 异常时 | `team/engine.py` `_on_io_fault`（永不抛） | 无 |
| 38 | **推进钩子询问点** | 模型收手（`not calls`）且信箱空之后、completed 之前 | `event_loop.py` `not calls` 分支（推进机制批次 1）；接口 `hooks/base.py` `AdvanceHook`/`advance_decide` | 不注册推进钩子即不触发（GA-4a/4b 注入钉住零值） |

### 读表要点

1. **只有 `max_rounds` 与 `GuardConfig.max_rounds` 是两层不同东西**：
   前者是**策略**（CLI 默认 20，SDK 默认 `None`=无上限，工作台 `None`），
   后者是**任何情况下都不许无限的绝对保险丝**（默认 200）。
   三个默认值各司其职（2026-10-06 拍板），**不是漂移**：
   产品壳替用户封顶、库层不替调用方做主、安全网兜底。
2. **第 5 与第 28 是压缩的两条触发路径**：一条按阈值主动压，一条是 provider 已经拒了才压。
   二者的可见性走同一个 `_last_compaction` 通道。
3. **第 13 的重试判据是"本轮零产出"**：产出过半个字再重试会让内容在树里出现两份。
4. **第 24 的截断理由不是省空间，是"最容易爆预算的地方"**（架构 5.3）。
5. **第 25/26 的差别**：持久化是正确性（丢了审计链就断了），trace 是可观测性（失败只停用观测）。

---

## 附表：模块 → 顶层类/函数

`src/sigma/` 共 97 个模块。下表按功能域列，粒度 = **文件 → 顶层类/函数**。
带 ✅ 的是主表里出现过的机制承载者。

### runtime（4）

| 文件 | 顶层类 / 函数 |
| --- | --- |
| `runtime/event_loop.py` | ✅ `AgentLoop`（唯一实现，`run_turn` 全项目仅此一处）· ✅ `TransientBackoff` · `_Planned` · `_StreamedRound` |
| `runtime/loop_guard.py` | ✅ `GuardConfig` · ✅ `LoopGuard` · ✅ `Verdict` · `CallFacts` · `normalize_args` · `result_fingerprint` · `NUDGE_TEXT` |
| `runtime/sub_agent.py` | ✅ `TaskTool` · ✅ `SubAgentRounds` · `TaskParams` · `_SubTaskState` · `SUB_SYSTEM_PROMPT_PREFIX` |
| `runtime/__init__.py` | — |

### hooks（3）· events（2）

| 文件 | 顶层类 / 函数 |
| --- | --- |
| `hooks/base.py` | ✅ `BaseHook` · ✅ `ApprovalHook` · ✅ `AdvanceHook` · ✅ `HookManager` · `DuplicateHookError` |
| `hooks/persist.py` | ✅ `SessionPersistHook` |
| `events/lifecycle.py` | `ApprovalDecision` · `AssistantProduced` · `MessageInjected` · `TextChunk` · `ThinkingChunk` · `LlmRequested` · `ToolStart` · `ToolEnd` · `TurnEnd` · `ApprovalDecided` · `AdvanceFacts` · `AdvanceDecision` · `AdvanceDecided` |

### security（4）

| 文件 | 顶层类 / 函数 |
| --- | --- |
| `security/path_sandbox.py` | ✅ `resolve_path` · ✅ `PathEscapesWorkspace` |
| `security/shadow_checkpoint.py` | ✅ `ShadowCheckpoint`（mark/restore/refs/水位治理/`startup_sweep`/`close_session`） · `CheckpointInfo` · `RestoreReport` |
| `security/approval.py` | ✅ `CliApprovalGate` · ✅ `Allowlist` · ✅ `Finding` · ✅ `DANGEROUS_PATTERNS` |
| `security/__init__.py` | — |

### sessions（9）

| 文件 | 顶层类 / 函数 |
| --- | --- |
| `sessions/context.py` | ✅ `SessionContext`（`build_messages`/`verify_resident_region`/`verify_resident_budget`/`rebuild_with_tools`/`apply_compaction`） · `ResidentRegionChanged` · `ResidentBudgetExceeded` |
| `sessions/tree.py` | `SessionTree`（只追加、分支、`path_to`） · `TreeCorrupted` · `UnknownNode` |
| `sessions/store.py` | `JsonlStore` · `NodeRecord` · `LoadResult` · `BadLineSkipped` |
| `sessions/compaction.py` | ✅ `CompactionPolicy` · ✅ `CompactionSummary` · ✅ `CompactionOutcome` · ✅ `needs_compaction` · `EmptySummary` · `SUMMARY_PROMPT` |
| `sessions/repair.py` | ✅ `repair_dangling_tool_results` |
| `sessions/resources.py` | `load_project_instructions` · `ProjectInstructions` · `AGENTS_MD_FILENAME` |
| `sessions/repo_map.py` | ✅ `build_repo_map` |
| `sessions/sessions.py` | `SessionInfo` · `SessionPreview` · 目录级操作 |
| `sessions/__init__.py` | — |

### memory（2）· observability（3）· skills（2）· config（3）

| 文件 | 顶层类 / 函数 |
| --- | --- |
| `memory/file_store.py` | ✅ `scan_memory` · `render_memory_index` · `memory_dir_for` · `MemoryEntry` · `MemoryScan` |
| `observability/trace.py` | ✅ `TraceHook` |
| `observability/timeline.py` | `build_timeline` · `TimelineReport` · `ToolView` · `RoundView` · `ApprovalView` |
| `skills/scanner.py` | `discover_skills` · `render_index` · `load_body` · `SkillMeta` · `SkillScan` |
| `config/resident_caps.py` | ✅ `RESIDENT_BUDGET_TOKENS`(5750) · ✅ `CAPS` · ✅ `caps_sum` · `MEASURED` |
| `config/settings.py` | 密钥解析（`resolve_tavily_api_key` 等） · `USER_CONFIG_DIR` |
| `eval_profile.py` | `EvalProfile`（B0/B1/B2/B3 消融档） |

### team（8）

| 文件 | 顶层类 / 函数 |
| --- | --- |
| `team/engine.py` | ✅ `TeamEngine` · ✅ `EngineConfig` · `IO_FAULT_WARN_AT` |
| `team/board.py` | `Board` · `TeamTask`（状态机迁移表即代码，纯逻辑零 I/O） |
| `team/store.py` | `BoardStore`（原子写 tmp+`os.replace` + 实例级 `asyncio.Lock`） |
| `team/mailbox.py` | ✅ `Mailbox`（追加一行、drain-once） · `MailMessage` |
| `team/lead_board.py` | ✅ `LeadBoard`（create/reclaim/abandon/cancel） |
| `team/worker_board.py` | ✅ `WorkerBoard`（claim/heartbeat/finish/fail） |
| `team/scanner.py` | ✅ 纯函数 tick（系统扫描器，非 agent） |

### tools（23）· agent（3）· providers（20）· cli（5）· prompts（2）

| 文件 | 顶层类 / 函数 |
| --- | --- |
| `tools/base.py` · `registry.py` · `truncate.py` · `shell.py` | ✅ `BaseTool` · ✅ `ToolRegistry`（`clone`/`load_extensions`/`reload_source`） · ✅ `truncate_output` · `ToolDefinition` · `ReloadReport` · `DuplicateToolError` |
| `tools/builtin/` | `ReadTool` `WriteTool` `EditTool` `BashTool` `GrepTool` `TodoTool` `AskUserTool` `LoadSkillTool` `WebSearchTool` `WebFetchTool` `TeamBoard` `MultiAgentTool` + 各自的 `*Params` |
| `tools/quota/` | `CreditLedger`（ABC） · `TavilyQuota` · `FirecrawlQuota` |
| `agent/messages.py` · `types.py` | ✅ `AgentMessage`(ABC) · ✅ `convert_to_llm` · `LlmMessageWrapper` · `ToolResultAgentMessage` · ✅ `ToolResult` · ✅ `ToolContext` · ✅ `TurnResult` |
| `providers/base.py` · `events.py` · `tool_calls.py` · `messages.py` · `errors.py` · `tokens.py` · `registry.py` · `fake.py` | `BaseProvider` · `CancelToken`/`InterruptToken`/`NeverCancelled` · `SamplingParams` · 六类 `StreamEvent` · ✅ `ToolCallAssembler` · 四消息四块 · `ErrorCode`/`ProviderError` · `estimate_text` · `ProviderRegistry`/`ProviderSpec` · ✅ `FakeProvider` |
| `providers/openai/` · `anthropic/` | `OpenAICompatProvider` · `AnthropicProvider` + 各自 `convert`/`protocol`/`sse` |
| `cli/main.py` · `repl.py` · `render.py` · `interactive.py` | ✅ `SessionManager` · ✅ `SessionBinding` · `SwitchOutcome` · ✅ `TerminalRenderer` · 按键选择器 |
| `prompts/system_prompt.py` | ✅ `SYSTEM_PROMPT` · ✅ `build_system_prompt` · 各工具行常量 |

`src/sigma/` 顶层另有 `__main__.py`（`python -m sigma`）、`sdk.py`（✅ `InteractiveSession` · `default_registry` · `scan_skills` · `web_search_tool` · `web_fetch_tool` · `run_task`）。

---

## 三处"文档比代码美"的历史坑（已改正，留档）

本节存在的理由：本索引本身就是一次盘点的产物，盘点时发现三处文档与代码不符。
记下来是为了**下次别再照着旧文案推理**。

1. **steering 的送达时机**（2026-10-06 改正）
   旧文案（`architecture.md` 4.3 / `设计逻辑.md` 图 2）：steering 在"工具批次执行完后……
   **打断本批次剩余工具**"。实现从来不是这样：`_drain_steering` 在**每轮开头、模型调用前**。
   **以实现为准**——打断批次会打破门槛 G27「结果与调用严格一一对应」。
   已改：两处文档 + 该节加了更正注记。

2. **`--no-builtin-tools` 根本不存在**（2026-10-06 改正）
   三处（`registry.py` 报错文案 / `extensions/README.md` / `architecture.md` 3 节 & 4.4 节）
   让用户"显式走 `--no-builtin-tools` 替换内置工具"，而 `cli/main.py` 的 28 个参数里
   没有它。照做 = `unrecognized arguments`。
   已改：三处文案改为如实表述（重名即硬拒，当前没有替换路径）。
   **给一个不存在的退路比没退路更差**——用户会以为是自己拼错了。

3. **常驻区预算数字的散落**（未完全收敛）
   现行值 **5750** 住三处且靠 G885 对账：`config/resident_caps.py:41`、
   `sessions/context.py:63`、`architecture.md` §5.1。
   但**旧值 3500** 仍散落在 `设计逻辑.md:54/97/109`、`sdk.py:11`、
   `docs/plans/` 多份详规（历史记录，可留）与 `decisions/README.md:25`（索引行，未标注修订）。
   `decisions/D4-上下文预算.md` 的标题行也还写着 3,500（正文有 v2/v3 修订记录）。
   **判据**：历史详规保留原值是对的（那是当时的记录）；**索引行与阅读入口**（ADR 标题、
   `decisions/README.md` 状态列）该写现行值或明确标注"见正文修订记录"。

---

## 维护约定

- **本表是索引，不是规格**。规格在 `docs/architecture.md`，决策依据在 `docs/decisions/`。
- 新增机制时**同时**更新主表一行；只加代码不加表，这张表就会退化成"曾经对过"的东西
  （本项目已有先例：`技术债登记.md` 记过一次"台账说已修、HEAD 却红"）。
- 行号会漂移。**认机制名与关闭方式，不要认行号**。
- "关闭方式"列空着的行，意思是**只能靠注入实验证伪**——那不是遗漏，
  而是"它一旦被改动，行为就变了"的如实标注。
