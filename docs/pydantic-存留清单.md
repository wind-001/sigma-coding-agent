# pydantic 存留清单（P6 批次C）

> 2026-09-29 ｜ 依据 AGENTS.md 第 3 条 v2（三问判定，星辰拍板 Q5）：
> **①要生成工具 JSON schema？②要落盘/跨进程序列化？③要校验外部输入？**
> 三问皆否 → frozen/普通 dataclass；任一成立 → BaseModel。
> 判定的审计基线：46 个 BaseModel 子类（43 直接 + 3 经 AgentMessage 间接）。
> 降级 17 个，存留 29 个。

## 存留的 BaseModel（29 个）

### 第 1+3 问成立：工具参数模型（12 个）——schema 自动生成是机制核心

| 类 | 位置 |
| --- | --- |
| BashParams / ReadParams / WriteParams / EditParams / GrepParams | tools/builtin/ 各自文件 |
| TodoParams / WebFetchParams / WebSearchParams / AskUserParams / LoadSkillParams | tools/builtin/ 各自文件 |
| TaskParams | runtime/sub_agent.py |

（唯一消费点：`runtime/event_loop.py` 的 `tool.params.model_validate(arguments)`
对 LLM 传参做**校验外部输入**；`tools/base.py` 的 `BaseTool.json_schema()`
经 `model_json_schema()` 进常驻区。**类名即 schema title，红线：不许改名**。）

### 第 2 问成立：落盘/序列化载体（16 个）

| 类 | 位置 | 序列化通道 |
| --- | --- | --- |
| AgentMessage（ABC）/ LlmMessageWrapper / ToolResultAgentMessage | agent/messages.py | `model_dump`/`model_validate` → 经 NodeRecord 落 JSONL（G2 往返门槛） |
| CompactionSummary | sessions/compaction.py | 经 message_to_dict 落 JSONL |
| NodeRecord | sessions/store.py | JSONL 会话树一行一节点 |
| Usage / ToolMeta / TextBlock / ThinkingBlock / ImageBlock / ToolCallBlock | providers/messages.py | 嵌套于消息落盘 |
| SystemMessage / UserMessage / AssistantMessage / ToolResultMessage | providers/messages.py | 同上（G9 字段集门槛钉 SystemMessage） |
| _TodoData | tools/builtin/todo.py | `.sigma/todo.json` 读写 |
| LedgerUsage | tools/quota/credit_ledger.py | 额度状态文件读写 |
| ProviderErrorPayload | providers/errors.py | **第 3 问**：直接从 HTTP 错误响应体构造（openai/provider.py 七处），raw 装原始响应 |

## 降级的 dataclass（17 个）

### frozen（8 个）——值对象/事件，一经构造不该被改

| 类 | 位置 | 三问皆否的依据 |
| --- | --- | --- |
| TextDelta / ThinkingDelta / ToolCallDelta / UsageEvent / StopEvent / ErrorEvent（kw_only） | providers/events.py | 生产不序列化（聚合出的 AssistantMessage 才落盘）；SSE 字段转换由 protocol.py 手工完成；G3 门槛改为 `replace` 全字段复制保真 |
| ApprovalDecision | events/lifecycle.py | 审批按键直接构造，不落盘（JSONL 里只有 details 影子） |
| QuotaDecision | tools/quota/credit_ledger.py | 进程内判定结果，落盘的是 LedgerUsage |
| SkillMeta | skills/scanner.py | frontmatter 必填校验是手写 if，pydantic 不承载 |
| ProviderSpec | providers/registry.py | 内置硬编码五条，无凭据（测试钉字段集） |

### 普通（9 个）——可变数据载体

| 类 | 位置 | 三问皆否的依据 |
| --- | --- | --- |
| SamplingParams / StreamOptions | providers/base.py | 进程内请求参数，请求体由 convert.py 手工拼 dict |
| ToolResult | agent/types.py | 工具在进程内构造，转正走 `ToolResultAgentMessage.from_result` |
| ToolContext | agent/types.py | DI 载体，持 CancelToken/Callable，纯进程内 |
| ToolDefinition | tools/base.py | 注册表内存元数据，装的是已生成的 schema dict |
| EvalProfile | eval_profile.py | 评测报告只写 name 字符串；`model_copy(update=)` 已换 `dataclasses.replace` |
| SubAgentRounds | runtime/sub_agent.py | 三档轮数配置值对象 |

## 降级牵出的两个显式转换点（性能与正确性等价，不是损失）

1. **providers/fake.py**：重建 transcript 事件时，`UsageEvent.usage` 与
   `ErrorEvent.error` 两个 pydantic 载体由 `model_validate` 显式重建——
   原先靠 pydantic 构造器隐式嵌套转换，降级后隐式行为消失，必须写明。
2. **evals/task_runner.py**：`model_copy(update=)` → `dataclasses.replace`。

## 计数

| | 批次C 前 | 批次C 后 |
| --- | --- | --- |
| BaseModel 子类 | 46 | 29 |
| dataclass | 34 | 51 |
