# P5-批次1:观测层(trace 文件 + 时间线查看器)详规

> 立项:2026-09-28 ｜ 需求方:星辰 ｜ 状态:**✅ 已落地(2026-09-28,G865–G872 全绿,注入 8/8 证伪,回归 710 passed)**
> 前置阅读:`architecture.md` 7.5(指标定义)/7.6(主张→验证对照)、`P4-批次5/6` 详规(钩子总线)、`P3-批次2-审批拦截层-详规.md`(格式参照)
> 定位:P5 的验收门是"7.6 八项主张全部有对照数据"——**指标先得有采集面**。本批是 P5 自证的地基,同时补上全项目"链路可观测"这块最短板。

## 0. 需求原话(星辰,2026-09-27/28)

> 我想要深入的了解这个项目是如何运转的,怎么观测他的链路呢……我希望在设计工具调用、skill、记忆、上下文管理、如何让系统稳定运行、**链路如何追踪、如何可观测**这些问题上有深度,当面试官深挖我的项目时,我希望我不仅仅知道这样设计,我更希望为什么这样设计,做了哪些取舍,解决了什么实际问题。

一句话:**给"每次 LLM 调用花了多少毫秒/多少 token、每个工具跑了多久、审批拦了什么、打断注入了几次"一个可查的家**——原始数据大半已在会话 JSONL 里,但没人采集延迟、没人留审批决策、没有查看器。

## 1. 现状与差距

| 观测面 | 现状 | 缺口 |
| --- | --- | --- |
| 事件流 | 7 种通知事件 + 审批决策钩子(hooks.py),"日志/渲染也是钩子义务" | 订阅者只有渲染与持久化,**没有记录者** |
| token | 每条 assistant 消息带 `usage`(含 `cached_tokens`),TurnEnd 汇总 | 已有,但**读不出来**(无查看器) |
| LLM 延迟 | **无**——没有"请求开始"事件,测不到单次调用耗时与首 token 延迟 | 本批主体 |
| 工具耗时 | ToolStart/ToolEnd 成对,但**没人计差值** | 本批主体 |
| 审批决策 | `_approval.py` 零留痕——拒绝只在工具结果里间接可见,allowlist 命中/危险模式完全不可追溯 | 本批主体 |
| 结构化日志 | **无**(全项目无 logger;evals 层有 wall_clock 但只在评测时采集) | trace 文件补位 |
| 查看器 | `--trace` 旗标已被占用(现有语义=结束时打印完整消息序列,调试用) | 新 `--timeline` |

**关键事实:会话 JSONL 里每条消息已有毫秒级时间戳与 usage**——所以查看器对**存量旧会话**也能工作(延迟用相邻消息时间戳差近似,标 `≈`);trace 文件提供精确值(单次调用延迟、TTFT)与 JSONL 里不存在的信息(审批决策)。

## 2. 方案

### 2.0 总体:两层,严格分开

```
①采集层  TraceHook(sigma_session/trace.py,新)
         订阅 8 种事件 → 逐事件 append 一行 JSON → <sessions_dir>/<session_id>.trace.jsonl
②查看层  --timeline CLI 旗标(core/sigma/timeline.py,新)
         会话 JSONL 为主 + trace 文件(存在则用) → rich 表格渲染
```

**trace 为什么是独立文件、不进会话 JSONL**:会话 JSONL 是**审计凭据**(store.py:"会话历史是事实记录"),trace 是**派生观测数据**——删掉它不损任何能力、不进模型上下文、不进 produced。这与 compact.py"压缩是派生视图、不改树"是同一条判据:**审计链与派生视图必须分家**。

### 2.1 新增两个通知事件(动核心的两处,各一行 emit)

按 hooks.py 判据 1:"新增时机 = 新增一个事件 dataclass + loop 里一处 emit,接口不变"。

```python
@dataclass(frozen=True)
class LlmRequested:
    """钩子点:即将发起一次模型请求。观测用途:与 AssistantProduced/首个
    TextChunk 配对,测单次调用延迟与首 token 延迟(TTFT)。
    为什么在 loop 发而不是 provider 测:sigma_ai 不认识钩子(import-linter
    契约),且回放/评测换 provider 时测量点不该漂移。"""

@dataclass(frozen=True)
class ApprovalDecided:
    """钩子点:一次审批询问的结论。name/arguments/call_id + decision
    (ApprovalDecision)。批准与拒绝都发——"allowlist 命中放行"与
    "命中危险模式被拒"都是需要追溯的观测事实。"""
```

- `loop.py` 接线:①`_stream_model` 调用前 emit `LlmRequested`;②`_execute_batch` 里 `approve_tool` 返回后 emit `ApprovalDecided`。
- **零钩子 = 行为不变**承诺不破:无订阅者时 `subscribers()` 返回空表,emit 是空转(与现有事件同款)。

### 2.2 TraceHook(`core/sigma_session/trace.py`,新文件)

```python
class TraceHook(BaseHook):
    name = "trace"
    # 订阅:LlmRequested / TextChunk(只计时,不写盘) / AssistantProduced /
    #       ToolStart / ToolEnd / MessageInjected / ApprovalDecided / TurnEnd
    # 构造注入:session_id、sessions_dir、now(字符串时间戳)、timer(monotonic 秒)
    #           ——timer/now 全部可注入,回放与单测确定性(FakeProvider 同款纪律)
```

- **写盘方式**:逐事件 open-append 一行 JSON(与 `JsonlStore` 同款;进程崩不丢已写行)。文本增量(TextChunk)**不落盘**——那是流式渲染的事,trace 只记"首个文本块到达时刻"用于 TTFT,随后该轮忽略。
- **行格式**(字段按 kind 裁剪,示例):

```json
{"ts":"2026-09-28T10:00:00.123","t":12.345,"kind":"llm_end","round":3,
 "model":"deepseek-chat","latency_ms":2340,"ttft_ms":890,
 "prompt_tokens":8100,"completion_tokens":220,"cached_tokens":6400}
{"kind":"tool_end","name":"bash","ok":true,"duration_ms":4120,"truncated":false}
{"kind":"approval","name":"bash","allowed":false,"reason":"命中危险模式:rm -rf ~;后果:…"}
{"kind":"turn_end","status":"completed","rounds":4,"prompt_tokens":26000,"completion_tokens":800}
```

- usage/model 从 `AssistantProduced.message`(LlmMessageWrapper)内层 `AssistantMessage` 取——用包装器的公开取法,**不重造包装逻辑**(hooks.py 既有判据)。`truncated` 从工具结果 `details` 取(键名实现期对齐 truncate.py,取不到则省略字段)。
- round 计数由 TraceHook 自己维护(`LlmRequested` 递增)——事件保持最小载荷,不带 round。

### 2.3 失败语义:自宽容(与持久化钩子**刻意相反**)

**TraceHook 落盘失败 = 警告一次 + 自禁用(墓碑行),不崩 turn。**

依据 hooks.py 既有分工:"渲染类钩子自己负责宽容(G34/G99:TerminalRenderer 内部降级),总线不代吞——**宽容是订阅者的策略,不是总线的**"。TraceHook 同属此类:

| | SessionPersistHook | TraceHook |
| --- | --- | --- |
| 数据性质 | 审计凭据 | 派生视图 |
| 失败若继续 | 内存领先磁盘 → 审计链**分叉**(不可接受) | 只少几行观测记录,无链可分叉 |
| 失败处置 | 抛出、中止本轮(G92:宁可崩) | try/except 内部消化,stderr 一行警告,`self._dead=True` 后续空转 |

这是面试叙事上最值钱的一对对照:**同一个总线里,"宁可崩"与"自宽容"并存,分界线是数据性质,不是工程方便**。观测挂了把任务也拖死,等于让可观测性降低可用性——方向反了。

### 2.4 时间线查看器(`core/sigma/timeline.py` + CLI `--timeline`)

```
$ sigma --timeline            # 最近一个会话
$ sigma --timeline <ID>       # 指定会话

会话 20260923-203059 ｜ 6 轮 ｜ prompt 18.2k / completion 2.1k ｜ cache 命中 71%

轮   LLM延迟   TTFT    tok 入/出/缓存          工具(耗时)
 1   2.3s      0.9s    8.1k / 220 / 6.4k       read(0.1s) grep(0.4s)
 2   ≈1.9s      —      9.0k / 180 / 7.2k       edit(0.2s)
 …
审批 1 次:bash"rm -rf ~/old" → 拒绝(危险模式) ｜ 注入 2 次(steering)
截断率 3/22 ｜ 工具错误 1 ｜ 总耗时 41s
```

- **数据源优先级**:trace 文件存在 → 精确延迟/TTFT;不存在(旧会话)→ 用会话 JSONL 相邻消息时间戳差近似,**显式标 `≈`、TTFT 留空**——旧会话立即可查,这是"原始数据早就在、缺观测层"的直接兑现。
- 指标口径与 `architecture.md` 7.5 对齐:轮数、每任务 token、cache 命中率(`cached/prompt`)、截断发生率、工具错误数。**不发明新指标名**——查看器读的就是 P5 ablation 要报的那些数。
- rich 表格,80 列内安全(踩坑记录:rich 80 列)。`--json` 输出机器可读版(给后续 ablation 脚本复用)。

### 2.5 接线与开关

- `sdk.py`:`SessionPersistHook` 注册之后紧跟注册 `TraceHook`(**默认开**)。注册序 = 派发序,持久化先落、trace 后记。
- CLI:`--no-obs` 整层关(脚本/评测场景);`--timeline` 是独立只读动作,不启动会话。
- 评测与子 agent:evals 用自己的 HookManager 不注册 TraceHook → 零成本(与渲染钩子同款承诺);子 agent 不透传(trace 不覆盖子 agent 轮次,见 §6)。

## 3. 改动清单

| 文件 | 改动 |
| --- | --- |
| `core/sigma_agent/hooks.py` | +`LlmRequested`、`ApprovalDecided` 两个 dataclass,入 `HookEvent` union |
| `core/sigma_agent/loop.py` | 两处 emit:`_stream_model` 前;`approve_tool` 返回后 |
| `core/sigma_session/trace.py` | **新文件**:`TraceHook`(自宽容、时钟全注入) |
| `core/sigma/timeline.py` | **新文件**:纯函数渲染(会话 JSONL + trace → 表格/JSON) |
| `core/sigma/sdk.py` | 注册 `TraceHook`(默认开) |
| `core/sigma/cli.py` | `--timeline [ID]`、`--no-obs`、`--json` |
| `tests/test_trace_hook.py`、`tests/test_timeline_view.py` | G865–G872 |
| `scripts/gate_injection_batch16.py` | 注入实验 |
| `docs/architecture.md` | v1.7 changelog:观测层一节 |

## 4. 门槛(可证伪:删掉对应实现,测试必须红)

| 编号 | 断言 | 注入 |
| --- | --- | --- |
| G865 | 一 turn 回放后 trace 文件逐事件成行,kind 序列与事件序一致 | 删 `on_event` 的 write → 行数断言红 |
| G866 | `latency_ms`/`ttft_ms` 精确等于注入 fake timer 的推进量(不真跑钟) | 删 `LlmRequested` 的 emit 或 TraceHook 计时 → 数值断言红 |
| G867 | trace 写入失败(路径指向不可写)→ **turn 照常完成**、stderr 恰一条警告、后续事件零尝试(墓碑行);对照 G92 刻意相反 | 删 try/except → 变成崩溃,G867 红 |
| G868 | 拒绝/放行都留 approval 行,拒绝行含 reason 与命中模式 | 删 `ApprovalDecided` emit → 行缺失红 |
| G869 | 查看器渲染**无 trace 文件的旧会话**:轮数/token/cache 命中率与手算一致,延迟带 `≈`、TTFT 空 | 改汇总逻辑(如只取最后一轮 usage,复刻 2026-09-21 那个 bug)→ 红 |
| G870 | 同一会话两份数据并存时,查看器用 trace 精确值而非近似值 | 删优先级分支 → 用了 `≈` 值,断言红 |
| G871 | **观测零污染**:trace 事件不进 produced/树/`effective_history`;常驻区指纹断言(G26 族)原样绿;`lint-imports` 3 条契约 KEPT | 把 trace 写进会话树 → G26 族红 |
| G872 | 确定性:同一回放场景跑两遍,trace(除真实时间戳字段外)逐字节相同 | 留真实时钟引用 → 两遍不同,红 |
| 回归 | 全套 pytest / mypy --strict / lint-imports / 回放 6/6 | — |

## 5. 风险

| # | 风险 | 处置 |
| --- | --- | --- |
| R1 | 每事件写盘开销 | 与会话持久化同成本类(同为逐事件 append);不注册即零成本;TextChunk 不落盘 |
| R2 | trace 文件膨胀无人清 | 单会话 KB–几十 KB 量级(远小于 shadow.git);随会话目录生命周期,**不为本批新增水位机制**(P5 收尾统一定清理策略) |
| R3 | 隐私:trace 含工具参数(bash 命令/路径) | 与会话 JSONL 同敏感级、同目录同生命周期、不外传;文档写明 |
| R4 | `--trace` 撞名 | 旧旗标不动(语义=结束打印消息序列);新查看器叫 `--timeline`(见待拍板 Q1) |
| R5 | 子 agent token 不入主会话 usage,trace 亦不覆盖 | 已知口径缺口,记 §6;P5 ablation 报告必须写明口径(与"压缩成本记 details"同族问题) |

## 6. 明确不做(本批边界,代替路径照给)

- **$ 成本换算**:只落数据字段(model + tokens),不做价格表——价格会腐烂,token 才是 7.5 口径。要算钱:P5 ablation 时离线后处理 trace JSONL。
- **子 agent 独立 trace**:子任务开销经信箱注入部分可见于主 trace;完整覆盖属"task 工具深度观测",另立批次。
- **OpenTelemetry / 结构化 logger / Web 面板**:trace JSONL 是自足格式,要导出 OTel → 写一个订阅同样事件的导出钩子(钩子是唯一扩展面,免费);要面板 → 后处理 `--json` 输出。
- **实时 TUI 时间线**:不做 TUI 的决定不翻案(P5-Textual 已回退留档)。

## 7. 待拍板(星辰,审阅本规后定)

| # | 问题 | 推荐 |
| --- | --- | --- |
| Q1 | 查看器旗标名 `--timeline`;旧 `--trace`(结束打印消息序列)是否顺手改名为 `--dump-messages`? | **不改**,避免破坏肌肉记忆与脚本;撞名只在文档里写清 |
| Q2 | trace 采集**默认开**(`--no-obs` 关)还是默认关? | **默认开**:可观测应是缺省态;成本≈每事件一行 JSON;敏感性与会话文件同级 |
| Q3 | trace 失败自宽容(§2.3)是否接受?它与 G92"宁可崩"表象相反 | **接受**:分界线=审计凭据 vs 派生视图,渲染钩子(G34/G99)已有同款先例 |
| Q4 | 审批留痕走新事件 `ApprovalDecided`(动 loop 一处 emit) | **做**:零核心改动的替代方案(从 ToolEnd 的"用户拒绝"preview 反推)会丢掉放行/allowlist 命中信息,观测价值减半 |
| Q5 | 批次定位:P5-批次1(观测层)编号是否成立(TUI 重构已回退,P5 编号复用) | 成立;不成立则改 P4-批次9,仅改文件名 |

## 8. 拍板记录(2026-09-28,星辰)

> 原话:"全按照推荐"。

| # | 决定 |
| --- | --- |
| Q1 | 旧 `--trace` 不改名;新查看器叫 `--timeline [ID]` |
| Q2 | trace 采集**默认开**,`--no-obs` 整层关 |
| Q3 | 失败语义=自宽容(警告一次+自禁用),与 G92 持久化"宁可崩"刻意相反,分界=审计凭据 vs 派生视图 |
| Q4 | 审批留痕走新通知事件 `ApprovalDecided`(loop 一处 emit);事件不带 arguments(reason 已含命中信息,参数落盘是隐私与体积双重负担) |
| Q5 | 编号 P5-批次1 成立 |


## 9. 实现记录(2026-09-28)

| 项 | 结果 |
| --- | --- |
| 新事件 | `LlmRequested`(loop `_stream_model` 前)+ `ApprovalDecided`(approve_tool 返回后);**空审批名单不发**——结构性直通不是决定,发了=每个评测工具调用一行无信息量 approval(Q4 的实现期收紧,"零钩子=行为不变"在观测上的延伸) |
| TraceHook | 逐事件一行 JSON,**每事件恰好取一次 timer**(latency=两次事件 t 之差恒成立,G866 的对齐前提);TextChunk 只计时不落盘;tool_start→tool_end 经 call_id 配对,unparsed(无 ToolStart)如实无时长;并发批次的 tool_end 落在整批收尾(发射点既有语义,如实记录、不挪——"所有 ToolStart 早于 ToolEnd"渲染承诺优先) |
| 失败语义 | 自宽容(Q3):序列化+IO 全在 try 内(序列化首版在 try 外,自审抓住);警告恰一次 + 墓碑行 + `_dead` 后零尝试;G867 与 G92 形成刻意对照 |
| 查看器 | `build_timeline/render_timeline/timeline_to_json` 纯函数;**包装 user 消息的 wrapper 不算轮**(首版把空 wrapper 当一轮,落盘加载测试抓住);无 trace=相邻消息时间戳差近似标 ≈、TTFT 留空;审批兜底=从 details 反推拒绝并如实注明"放行不可见" |
| CLI | `--timeline [ID]`(nargs="?" const="",与回滚同列:不启动模型、排在 key 检查前)、`--json`、`--no-obs`;透传链 main → _run_once/run_task、_run_interactive → SessionManager → _build → InteractiveSession(enable_trace);子 agent 工厂 enable_trace=False(R5) |
| 碰撞修复 | `<id>.trace.jsonl` 撞 `list_sessions` 的 `*.jsonl` 通配 → 幽灵会话(实测 7 条测试红:/sessions 出现 `xxx.trace` 条目);处置=`TRACE_SUFFIX` 常量落 sessions.py(与它必须避让的 SESSION_SUFFIX 同址),发现层排除 |
| 测试 | `test_trace_hook.py` 8 条 + `test_timeline_view.py` 3 条;**G871 的正确断言点=重载 skipped_lines**(坏行不进 `_corrupt`,`clean`/节点数看不出污染——首版断言错落点导致 E871 注入不红) |
| 注入实验 | `scripts/gate_injection_batch16.py` **8/8 证伪**(E865 退订/E866 删锚点/E867 失败穿透/E868 删留痕/E869 只装最后一轮/E870 丢弃精确值/E871 写进会话文件/E872 绕开注入钟) |
| 回归 | **710 passed**(699+11)、mypy --strict 63 文件零错误、lint-imports 3 KEPT、回放 6/6 |
| 真机冒烟 | `sigma --timeline` 对 2026-09-27 旧会话:2 轮、cache 38%、截断 1、≈延迟渲染正常;`--json` 字段与 7.5 口径同名 |
| 常驻区代价 | **0 token**——观测层不进提示词、不进常驻区、不进模型上下文(G871) |

**踩坑记录**:① "同目录同名不同后缀"的文件设计,必须同时检查**谁在 glob 这个目录**——发现层的通配不认识新后缀,幽灵会话就是这么来的;② `skipped_lines` 与 `_corrupt` 是两条通道(坏行没有节点 id 可归),污染断言要选对落点;③ E871 首版注入"仍然全绿"——这正是注入实验存在的理由:门槛声称防的失败模式与断言落点不一致时,绿是假绿。
