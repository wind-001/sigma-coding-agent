# pydantic 存留清单（P6 批次C 交付物）

> 2026-09-29 ｜ 判定器：三问判定（拍板 Q5）——
> ①要生成 JSON schema？②要跨进程/落盘序列化？③要校验外部输入？
> **任一成立 → 留 pydantic，并在下表写明哪一问、证据在哪；三问皆否 → 降 dataclass。**

结果：**全库 42 个 BaseModel → 留 36 / 降 6**。
（详规 §1.2 预估"43→30"基于未审计的粗分；逐类核对后流事件等大量候选有明确
序列化证据，按判据留任。数字以本表为准，此为偏差之一，已记详规 §7。）

## 降级名单（6 个，全部三问皆否）

| 类 | 新形态 | 所在 | 降级理由（一句话） |
| --- | --- | --- | --- |
| `ToolContext` | dataclass | agent/types.py | 裹着 CancelToken/Callable，pydantic 只会逼出 arbitrary_types_allowed 逃逸舱 |
| `ToolResult` | dataclass | agent/types.py | 落盘的是逐字段拷贝它的 ToolResultAgentMessage，本体是瞬时的两段式载体 |
| `ApprovalDecision` | frozen dataclass | events/lifecycle.py | 构造方全在 harness 内；trace 只取字段拼自己的 dict |
| `SubAgentRounds` | frozen dataclass | runtime/sub_agent.py | 三档实测常量，无 schema/无落盘/无外部输入 |
| `EvalProfile` | dataclass | eval_profile.py | 档位由代码与命令行声明，报告里只写 name 字符串 |
| `QuotaDecision` | frozen dataclass | tools/quota/credit_ledger.py | 落盘的是 LedgerUsage，判定结果是瞬时产出 |

## 存留名单（36 个，按成立的问题分组）

### ① JSON schema 生成（12 个）——工具参数模型 + 定义载体

`AskUserParams` `BashParams` `EditParams` `GrepParams` `ReadParams`
`LoadSkillParams` `TodoParams` `WebFetchParams` `WebSearchParams`
`WriteParams` `TaskParams` `ToolDefinition`

证据：schema 由参数模型自动生成并进常驻区（D4 预算表 G885 逐字节锚定）；
loop 第 5 步 `tool.params.model_validate(arguments)` 是运行时校验闸
（event_loop.py）。**类名冻结红线**（风险 R2）：改名即变 schema 字节。

### ② 落盘/跨进程序列化（22 个）

**LLM 消息与块（10）**：`SystemMessage` `UserMessage` `AssistantMessage`
`ToolResultMessage` `TextBlock` `ThinkingBlock` `ImageBlock` `ToolCallBlock`
`Usage` `ToolMeta`
证据：会话 JSONL 落盘（`convert_to_llm`/`message_from_dict` round-trip，
agent/messages.py）；协议字段保真是测试契约。

**流事件（6）**：`TextDelta` `ThinkingDelta` `ToolCallDelta` `UsageEvent`
`StopEvent` `ErrorEvent`
证据（批次C 审计新增，推翻详规 §1.2 "超配"初判）：
transcript JSONL 回放就是从磁盘反序列化事件（fake.py `builders[type](**raw)`）；
G3 六类 round-trip + ValidationError 拒收是常驻测试（test_sigma_ai_events.py）。
**降 dataclass = 确定性回放地基失效。**

**会话/状态落盘（3）**：`NodeRecord`（会话树 JSONL，store.py）
`_TodoData`（todo.json）`LedgerUsage`（额度账本 JSON）——皆有
`model_dump_json`/`model_validate` 落盘闭环。

**协议载体（3）**：`SamplingParams` `StreamOptions`（进请求体 JSON）
`ProviderErrorPayload`（G3 round-trip + `ProviderError.from_payload` 还原通道）。

### ③ 外部输入校验（2 个）

`SkillMeta`（SKILL.md frontmatter——磁盘上的用户文件，字段错误要报得清楚，
不能静默错构）、`ProviderSpec`（.env/用户提供的连接配置）。

## 门槛对应

- 本清单变更的 6 个类均不进 schema、不进常驻区——**工具 schema 快照逐字节不变**
  （G-P6-4 同源判据，pytest 的 test_resident_budget 家族自动断言）。
- 全量门槛：pytest 724 / mypy strict / lint-imports 2/2（批次C 提交时实测）。
