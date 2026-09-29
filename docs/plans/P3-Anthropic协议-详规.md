# P3-Anthropic 协议 详规

> 版本:v1(拍板代行) ｜ 2026-09-29
> 授权:星辰 2026-09-29"多 agent 完成项目剩余任务"全权委托;协议范围依据 architecture
> §2.2 / 拍板 a3——**"先 1 套(OpenAI 兼容),后 1 套(Anthropic),共 2 套封顶"**,
> "P4 之后"的时点已到(P5/P6 完成)。registry.py docstring 亦早已预留此位
> ("需要增加'用哪个实现类'的字段;现在不加,因为没有第二个实现可填")。

## 1. 范围

**做**:Anthropic Messages API 的流式 Provider(`BaseProvider` 第二实现),离线全测。
**不做**(v1 显式,避免范围失控):prompt caching 控制参数、vision(ImageBlock
类型已有, Anthropic 侧 v1 不主动发)、extended thinking 的开启参数
(ThinkingBlock 与 `*_signature` 字段**保真透传**但 v1 不主动请求)、batches、
prompt 前缀缓存开关。这些每一个都是"能跑但说不清"的负资产,留给有真实需求时。

## 2. 文件布局(镜像 openai/,已验证的拆分方式)

```
src/sigma/providers/anthropic/
├── __init__.py     # 导出 AnthropicProvider;模块 docstring 写"为什么直连 httpx"
├── protocol.py     # stop_reason 映射 / usage 映射 / 错误体解析与归一化
├── convert.py      # LlmMessage 列表 → Anthropic 请求体(本批的核心难点,见 §3)
├── sse.py          # SSE 分帧(Anthropic 事件比 OpenAI 多一层 event: 类型行)
└── provider.py     # AnthropicProvider(BaseProvider 第二实现)
```

## 3. 请求体转换(与 OpenAI 的四个结构差异,convert.py 的全部难点)

| # | 差异 | 处置 |
| --- | --- | --- |
| 1 | **system 是顶层参数**,不是 message | `SystemMessage.content` → 请求体 `"system"` 字段;P1 设计保证只会有一条(4.0.4),多条即 `ValueError`——宁可崩不要错 |
| 2 | **tool_result 是 user 消息里的 content block**,不是独立 role | `ToolResultMessage` → `{"role":"user","content":[{"type":"tool_result","tool_use_id":…,"content":[{type:text,…}],"is_error":…}]}`;连续的 tool_result 合并进同一条 user 消息 |
| 3 | **assistant 的工具调用在 content 里**(tool_use block),且 arguments 是**对象**不是字符串 | `ToolCallBlock.arguments` → `{"type":"tool_use","id","name","input":arguments}` |
| 4 | 工具 schema 字段名不同 | `input_schema`: 直接用 `model_json_schema()` 产物;`cache_control` v1 不加 |

其余:user/assistant 文本块按序透传;`thinking` 块 v1 **丢弃并计数可见**
(未开启 extended thinking 时 Anthropic 本来不会返回;防御性丢弃要留痕,
与"丢弃必须可见"纪律同源)。消息序列必须严格 user/assistant 交替——
 sigma 的树形历史天然满足;违反即 ValueError(列出冲突消息 role 序列)。

## 4. 流式(SSE)与事件映射

请求体:`"stream": true`、`max_tokens` 必填(Anthropic 无默认值,取
SamplingParams.max_tokens,缺省 4096——在 docstring 写明这是协议要求不是模型默认)。
响应事件序(逐个映射成 StreamEvent,复用 `providers/events.py` 六类,**不新增事件类型**):

| Anthropic 事件 | → StreamEvent |
| --- | --- |
| `message_start`(带 usage.input_tokens / cache_read_input_tokens) | (记录,不发) |
| `content_block_start`(type=text / tool_use 带 id/name) | (tool_use:记录 id/name) |
| `content_block_delta` type=`text_delta` | `TextDelta(text=…, text_signature=…)` |
| `content_block_delta` type=`input_json_delta` | `ToolCallDelta(index=block 序,id,name,arguments_delta=partial_json)` |
| `content_block_delta` type=`thinking_delta` | `ThinkingDelta` |
| `message_delta`(delta.stop_reason + usage.output_tokens) | `UsageEvent` → `StopEvent` |
| `message_stop` | 正常结束的判定锚(**断流检测**对齐 openai 侧 saw_finish 语义) |
| `error`(流中途) | `ErrorEvent(ProviderErrorPayload)` |

**usage 映射**:prompt=input_tokens;completion=output_tokens;
cached=cache_read_input_tokens(缺省 0)。
**stop_reason 映射**:end_turn/stop_sequence→`stop`;tool_use→`tool_use`;
max_tokens→`length`(映射表放 protocol.py,单测逐行钉住)。
**错误归一化**(对齐 `openai/protocol.py` 的 ErrorCode 家族):429→`rate_limit`;
`overloaded_error`(529)→`transient`;`invalid_request_error` 且消息含
"context length/prompt is too long"→`context_overflow`;认证失败→`auth`;
其余→`transient`。头部:`x-api-key` + `anthropic-version: 2023-06-01`(常量)。

## 5. registry 与 CLI 接线

- `ProviderSpec` 增加 `protocol: Literal["openai-compat", "anthropic"] = "openai-compat"`
  (带默认值 → 现有五条 spec 与所有调用点零改动;registry docstring 的预留位兑现);
- builtin_providers 追加 `anthropic` 条目(base_url=`https://api.anthropic.com`,
  default_model=`claude-sonnet-4-5`,protocol="anthropic")——默认模型只是
  预设,`--model` 随时可覆盖,docstring 写明;
- `_make_provider` 按 `spec.protocol` 选择实现类;`--preset anthropic` 即走新协议;
- `sdk.run_task`/`InteractiveSession` 已按 provider 注入,**零改动**(抽象的收益现场兑现)。

## 6. 门槛(全部离线,镜像 openai_compat 测试结构)

| 编号 | 断言 |
| --- | --- |
| G-ANTH-1 | 消息映射:system 顶层化 / tool_result 归 user 合并 / assistant tool_use 形状 / 交替违反即抛 |
| G-ANTH-2 | SSE 分帧与增量拼装:text_delta 聚合、input_json_delta → arguments 完整 JSON |
| G-ANTH-3 | usage 映射含 cached;缺 usage 不炸 |
| G-ANTH-4 | stop_reason 四值映射逐行钉住 |
| G-ANTH-5 | 错误归一化五分支;ErrorEvent 载荷可 roundtrip(ProviderErrorPayload 已是 pydantic) |
| G-ANTH-6 | 断流检测:无 message_stop 即截断语义,与 openai 侧同口径 |
| G-ANTH-7 | 工具 schema 形状(input_schema 直挂);多工具并行 index 归属正确 |
| G-ANTH-8 | registry:anthropic 可解析、protocol 字段默认值不破坏既有五条;`_make_provider` 按 protocol 分派 |

回归门禁:pytest 全绿(735+新增)、mypy --strict 零错、lint-imports 2/2、
`FakeProvider` 与 openai 路径零改动(G-P6-4 同款"新功能不改既有字节")。
**禁止真实联网调用**——全部经 MockTransport / 合成 SSE。

## 7. 不拍的板(留给星辰)

- default_model 用哪个 claude 型号只是预设值,随时 `--model` 覆盖,不影响结构;
- 是否为评测集加 anthropic 臂(要真钱,与波 2 一并决策)。
