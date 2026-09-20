# P1 重构详规：`sigma_ai` 与继承制（含 a2 的重新审视）

> 依据：`docs/architecture.md` 2/3/4 节、第 8 节门禁表；`docs/plans/P0-骨架.md` 第 6 节 a2
> 状态：**待本人审阅**（`AGENTS.md` 第 7 条——**本次未改任何实现代码**）
> 提出日期：2026-09-20
> 起因：本人提出「`sigma_ai` 的文件夹设计不合理、完全按继承制设计过于繁杂、
> 希望保持接口清晰的同时更简练、可以使用 Protocol、
> `sigma_ai` 应封装 OpenAI 协议的 LLM 调用 / tool 调用 / message 封装」

---

## 0. 先摆事实：三个与直觉不一致的量化结果

在讨论怎么改之前，先把现状量出来。**否则"繁杂"是个感觉，不是问题。**

### 0.1 抽象面其实很小——"完全按继承制设计"不成立

| 抽象基类 | 抽象方法数 | 实现者数 | 判断 |
| --- | --- | --- | --- |
| `BaseProvider` | 2 | **2**（`FakeProvider` / `OpenAICompatProvider`） | **合理**——G10 正是靠"两个实现者"逼出了签名缺漏 |
| `CancelToken` | 2 | 1（`NeverCancelled`） | 可接受（P3 会有真实实现） |
| `BaseTool` | 2 | 2（`ReadTool` / `WriteTool`，最终 5 个） | **合理**——多实现 |
| `BaseLoop` | 1 | **1**（`AgentLoop`） | **多余**（架构自己写"只允许一个子类"） |

**全项目只有 4 个 ABC、7 个抽象方法。** `sigma_ai` 里只有 **2 个**（`BaseProvider` / `CancelToken`）。

所以「完全按照继承制来设计」这个描述**与事实不符**。继承制用得不多，
真正的问题在别处（见 0.3）。

### 0.2 `sigma_ai` 规模：1484 行 / 8 文件

| 文件 | 行数 | 占比 |
| --- | --- | --- |
| `openai_compat.py` | **534** | **36%** |
| `messages.py` | 202 | 14% |
| `errors.py` | 182 | 12% |
| `fake.py` | 178 | 12% |
| `base.py` | 154 | 10% |
| `tokens.py` | 114 | 8% |
| `events.py` | 104 | 7% |
| `__init__.py` | 16 | 1% |

**`openai_compat.py` 一个文件占了三分之一。** 这里装的其实是四件不同的事：
HTTP 客户端、SSE 分帧、消息转换（LLM 层 → 请求体）、事件翻译（响应 → `StreamEvent`）。
**这是"繁杂"真正的来源之一。**

### 0.3 真正的问题只有三个

| # | 问题 | 证据 | 性质 |
| --- | --- | --- | --- |
| **A** | `BaseLoop` 是"为了继承而继承" | 只有 1 个子类，架构自己规定不许有第二个 | **设计冗余** |
| **B** | `openai_compat.py` 534 行承担四件事 | 见 0.2 | **内聚度不足** |
| **C** | `tool_calls` 的协议处理散在两处 | 分片归属在 `openai_compat.py`，拼装成调用在 `loop.py` | **职责边界模糊** |
| **E** | **Provider Registry 完全缺失** | 架构第 248 行写明 `sigma_ai` 的职责含 "Provider Registry"，实现里没有；provider 列表反被硬编码在 `sigma/cli.py` 的 `PRESETS` | **实现遗漏 + 分层错误** |

**下面逐项处理。每一项都单独可决策——不需要整体接受或整体否决。**

---

## 1. 关于 a2：我当时的论证有一个漏洞，需要更正

a2（2026-09-20 拍板）写的是「一律继承制 `abc.ABC`，不用 `typing.Protocol`」，
理由记在 `core/sigma_ai/base.py` 的 docstring 里：

> Protocol 是结构化类型，不产生继承关系，忘实现抽象方法时只在**首次调用**
> 才暴露；ABC 在**实例化**就抛 `TypeError`。「后期扩展」这个诉求下，
> 早暴露远比晚暴露重要。

**这个论证漏了一个前提**：它假设项目**没有静态类型检查**。

而本项目的 CI 里有 **`mypy --strict`**（门禁第二条）。于是实际的暴露时机是：

| 方案 | 忘了实现时，什么时候被发现 |
| --- | --- |
| `Protocol` | **CI 静态检查阶段**（`mypy` 直接报"不满足协议"） |
| `ABC` | **运行期实例化时**（且只有那个类真被实例化才报） |

**Protocol 的暴露时机更早，不是更晚。** a2 的结论方向反了。

还有一个更隐蔽的点：**ABC 的 `TypeError` 只在"那个类真的被实例化"时才触发**。
如果某个实现类因为分支没走到而从未实例化，ABC 什么都不会说。
**静态检查没有这个盲区。**

### 1.1 但 Protocol 有真实代价，不能无条件换

| 代价 | 说明 |
| --- | --- |
| **失去"实例化即报错"** | Protocol 不能被实例化，所以没有这个运行期信号 |
| **`__subclasses__()` 不再可用** | 门槛 **G30**（唯一 loop 契约）依赖它 |
| **`isinstance` 需要 `@runtime_checkable`** | 且只检查方法名存在，**不检查签名** |
| **不能承载实例状态** | Protocol 类不能有 `__init__` 定义的实例字段（但可以有类属性） |

**受影响的门槛：G22（基类直接实例化抛 TypeError）、G23（工具都是 BaseTool 子类）、G30（唯一 loop 契约）。**

**注意**：这三条门槛是 a2 拍板时**专门为"让继承制落地"新增的**（架构 8 节原话：
"风格要求会随时间腐化，把它变成两条可执行的断言，才是让'继承制'真正落地的唯一办法"）。

**所以换 Protocol 等于拆掉这三条门槛**——而它们的存在理由会随 Protocol 一起消失。
这不是"门槛失效"，是"门槛的对象没了"。**这个连带关系必须想清楚再动手。**

---

## 2. 子项 A：删掉 `BaseLoop`（建议做）

### 现状

```python
class BaseLoop(ABC):
    @abstractmethod
    async def run_turn(self, messages: list[AgentMessage]) -> TurnResult: ...

class AgentLoop(BaseLoop):   # ← 唯一的子类
    ...
```

架构 4.3 节自己写着：「**当前只允许一个子类**（`AgentLoop`），不得并行存在第二个实现」，
并用门槛 G30 断言 `BaseLoop.__subclasses__()` 恰好只有 `AgentLoop`。

### 判断

**这是纯粹的冗余**：一个只允许有一个子类的抽象基类，等价于"给唯一的实现加一层间接"。

架构给的保留理由是「将来若要做"两阶段规划循环"或"反思循环"，不必改动公共接口」——
但那个"将来"没有任何具体需求，而**代价是现在就存在**：

- 多一个类型要维护、多一条门槛要跑、多一层跳转要看
- 更重要的是：**它让"继承制"看起来比实际用得多**，这正是本次提出重构的直觉来源

**这与架构 4.3 节自己的原则冲突**：那里说「不做没有需求的设计」（"取消者"原则的同源应用）。

### 方案

- 删除 `BaseLoop`
- `AgentLoop` 直接实现 `run_turn`
- **G30 改为**：「全项目只有一个定义了 `run_turn` 的循环类」——
  用模块扫描或直接断言 `loop` 模块的公开名字集合

**代价（写出来）**：将来真要做第二种循环策略时，要么重新引入基类，
要么用 `Protocol`（那时正好有真实需求，选择依据比现在充分）。

---

## 3. 子项 B：拆 `openai_compat.py`（建议做）

### 现状

534 行、承担四件事：

| 职责 | 大致内容 |
| --- | --- |
| `finish_reason` 映射 | `_FINISH_REASON_MAP` + `_map_finish_reason` |
| 消息转换 | `message_to_openai` / `_blocks_to_openai_content` |
| SSE 解析 | `parse_sse_line` |
| Provider 本体 | `OpenAICompatProvider`（HTTP、流式循环、事件翻译） |

### 方案

```
core/sigma_ai/openai/
    __init__.py          # 只导出 OpenAICompatProvider
    protocol.py          # finish_reason 映射、错误体解析 —— 纯协议知识
    convert.py           # LLM 消息 → 请求体 —— 纯转换
    sse.py               # SSE 分帧与解析 —— 纯解析
    provider.py          # OpenAICompatProvider —— 组装上面三块
```

**为什么这样切**：四块的**依赖方向是线性的**，且前三块都是**纯函数**——
「入参 → 出参，不碰网络、不持有状态」，因此**可以被单测直接打**，不必起 HTTP。

**顺带收益**：现有单测里那些"为了测一个纯函数而必须构造 Provider"的地方可以简化。

**不改**：对外接口 `OpenAICompatProvider(base_url=..., api_key=...)` 一个字符都不动。

---

## 4. 子项 C：`tool_calls` 的协议处理内聚（建议做，但边界要定清）

### 现状

一次工具调用从协议到可执行，跨了两层：

| 步骤 | 现在在哪 | 该在哪 |
| --- | --- | --- |
| 分片按 `index` 归属累积 | `openai_compat.py`（provider 层） | ✅ 协议层 |
| 拼成完整 `arguments` 字符串 | `openai_compat.py` | ✅ 协议层 |
| `json.loads` 解析 | **`loop.py`（agent 层）** | ⚠️ 待定 |
| 缺 `name` 的处置 | `loop.py` | ⚠️ 待定 |
| schema 校验 | `loop.py` | ❌ **agent 层**（要拿 `tool.params`） |

### 建议

在 `sigma_ai` 新增 `tool_calls.py`，把**协议层能判定的部分**收进去：

```python
@dataclass
class AssembledCall:
    """分片拼装的结果。**不是**协议类型，是拼装产物。"""
    index: int
    id: str | None
    name: str | None
    raw_arguments: str
    arguments: dict[str, Any] | None   # None 表示 JSON 解析失败
    parse_error: str                   # 非空表示失败原因
```

由 provider 层在流结束时产出 `list[AssembledCall]`，`loop` 只负责：
1. 缺 `name` / 解析失败 → 生成 `is_error` 结果（**agent 层语义**）
2. schema 校验（需要 `tool.params`，只能在 agent 层）

**边界判据**（写下来，供 P3 复核）：
> **协议层负责"把字节流变成结构化数据"；agent 层负责"这个调用该不该执行、失败了怎么办"。**

**这条判据的代价**：`AssembledCall` 要从 `sigma_ai` 导出，于是 `sigma_ai` 多了一个
"不是协议类型、也不是消息类型"的产物类型。**它的归属是有争议的**——
如果你认为它属于 agent 层，那子项 C 就不该做。

---

## 5. 关于「`sigma_ai` 应封装 tool 调用」——一处必须澄清的边界

你说 `sigma_ai` 应该实现「OpenAI 协议的 LLM 调用封装、**tool 调用封装**、message 封装」。

前两项与第三项没有争议（message ✅ 已有、LLM 调用 ✅ 已有）。
**但"tool 调用封装"有两种含义，必须分开：**

| 含义 | 该不该进 `sigma_ai` | 理由 |
| --- | --- | --- |
| **协议侧**：`tool_calls` 字段解析、分片拼装、tool schema 的形状 | ✅ **应该**（即子项 C） | 这是 wire protocol 的知识 |
| **执行侧**：调用工具、传 `ToolContext`、处理失败 | ❌ **不能** | 见下 |

**"执行侧不能进 `sigma_ai`" 的理由是分层契约，不是偏好**：

1. `sigma_ai` 是**最底层**，契约禁止它 import 任何其他 sigma 包。
   而工具执行需要 `ToolContext`（含 `workspace_root` / `signal` / `emit`），
   那是 `sigma_agent` 的类型。
2. 把 `ToolContext` 挪进 `sigma_ai` 会把"文件系统、取消信号、进度回调"这些
   **agent 层概念**拖进协议层，`sigma_ai` 就不再是"只管协议"了。
3. 架构 4.2 节两段式（`BaseTool` 行为 + `ToolDefinition` 元数据）已经把
   执行入口放在 `sigma_agent`，这条边界是**有意的**。

**所以：如果你要的是"执行侧也进 `sigma_ai`"，那是另一个决策，需要同时改分层契约。**

---

## 6. 关于「先定义、再实现」——这条完全同意，且现状已经基本符合

`sigma_ai` 当前的顺序是：`base.py`（定义 `BaseProvider`）→ `openai_compat.py` / `fake.py`（实现）。

**但有一处不符**：`messages.py` / `events.py` / `errors.py` 是**先有类型、后有实现**，
这符合原则；而 `tokens.py`（`estimate_messages`）是**实现先于任何定义**——
它没有抽象、没有接口，是个纯函数工具。

**建议**：本次重构顺手把 `tokens.py` 的定位写明（它是"估算工具"，不是"可替换的组件"），
**不引入没有需求的抽象**。

---

## 6.5 子项 E：补 Provider Registry（新增，2026-09-20）

### 事实

架构方案第 248 行的"分层职责表"写着：

| 层 | 职责 |
| --- | --- |
| 1 | **Provider Registry · 事件流 · 消息变换** |

**事件流**（`events.py`，6 类事件）与**消息变换**（`message_to_openai` /
`convert_to_llm` / 编解码）都有了。**Provider Registry 一行都没写。**

而它本该管的东西现在在哪儿：

```python
# core/sigma/cli.py —— 产品壳层
PRESETS: dict[str, tuple[str, str]] = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "moonshot": (...), "zhipu": (...), "dashscope": (...), "ollama": (...),
}
```

**这是分层错误**：`sigma` 是产品壳，`sigma_ai` 才是协议层。
"有哪些 provider、它们的 base_url 与默认模型是什么"属于**协议层的知识**，
放在壳层意味着：

- 换一个入口（直接调 `sdk.run_task`、批次 5 的评测运行器）就得再抄一份列表；
- P4 的扩展系统想注册自定义 provider 时，**没有落点**。

### 方案

```python
# core/sigma_ai/registry.py
class ProviderRegistry:
    """provider 的按名解析。

    P1 只做「名字 → 构造参数」的登记与解析，**不做热重载**（属 P4）。
    """

    def register(self, name: str, *, base_url: str, default_model: str,
                 aliases: Sequence[str] = ()) -> None: ...
    def resolve(self, name: str) -> ProviderSpec: ...
    def names(self) -> list[str]: ...
```

`cli.py` 的 `PRESETS` 迁进去，CLI 改为从 registry 取。

### 我自己提的反对意见

> "P1 只有两个 provider（Fake / OpenAI），做 registry 是不是过度设计？"

**不是**，三条理由：

1. **那个分层问题现在就存在**，不是"将来会有"——`PRESETS` 已经在壳层了；
2. registry 本身很小（约 40 行），是**纯数据登记 + 查表**，没有机制复杂度；
3. 它是 **P4 的必需品**，现在做成本最低——等 P4 再加，得反过来改 CLI 与评测。

**但要说清边界**：P1 的 registry **不做**热重载、不做 OAuth、不做成本追踪。
那三样是 Pi 的 `pi-ai` 有、而 sigma 明确不做的（见研究笔记第 10 节）。

### 与 `ToolRegistry` 的关系

**不复用同一个类**。两者形态不同：

- `ToolRegistry` 存的是**有行为的实例**（`BaseTool`）
- `ProviderRegistry` 存的是**构造参数**（provider 实例由调用方按配置创建，
  因为认证、超时、`httpx.AsyncClient` 注入都在构造期）

**共用抽象会把两件不同的事硬捏成一个。**

---

## 7. 决策清单

**四项独立可决策。我的建议列在第三列。**

| # | 项 | 我的建议 | 不做的代价 |
| --- | --- | --- | --- |
| **A** | 删除 `BaseLoop` | ✅ **做** | 保留一个永远只有 1 个子类的基类 |
| **B** | 拆 `openai_compat.py` 为 `openai/` 包 | ✅ **做** | 534 行单文件继续膨胀 |
| **C** | `tool_calls` 协议处理内聚进 `sigma_ai/tool_calls.py` | ✅ **做** | 职责边界继续模糊 |
| **D** | **a2 修订**：`Protocol` 与 `ABC` 并用 | ✅ **已确认：改成判据式**（不改现有实现） | 见下 |
| **E** | 补 `ProviderRegistry`（架构第 248 行规划的职责） | ✅ **建议做** | provider 列表继续错放在产品壳层；P4 无落点 |

### 7.1 关于 D（a2 修订），我的具体建议

**不建议整体推翻 a2**，而是**把 a2 从"一刀切"改为"按判据选"**：

| 场景 | 用什么 | 判据 |
| --- | --- | --- |
| 有**多个实现**、且需要**共享默认实现或类属性** | `ABC` | 继承带来实际复用 |
| 只有**协议约定**、实现者**不需要共享代码** | `Protocol` | 继承只剩形式 |
| 需要**运行期 `isinstance` 判定**（注册表、门槛） | `ABC` | Protocol 的 isinstance 不查签名 |

**按这个判据重新看现有的 4 个 ABC**：

| 类 | 现状 | 建议 | 理由 |
| --- | --- | --- | --- |
| `BaseProvider` | ABC | **保持 ABC** | 有 2 个实现，且 `BaseProvider` 承载了"签名即契约"的门槛 G10 |
| `BaseTool` | ABC | **保持 ABC** | `ToolRegistry.register(tool: BaseTool)` 靠运行期类型约束；G23 靠 `__subclasses__` |
| `CancelToken` | ABC | **保持 ABC** | P3 会有多种实现且共享"两个方法"的约定 |
| `BaseLoop` | ABC | **删除**（子项 A） | 只有 1 个子类 |

**结论：按新判据逐项检查后，没有一项需要改成 `Protocol`。**

于是 D 的实际影响是：**修订 a2 的表述（从"一律 ABC"改为"按判据选"），
但不改变任何现有实现。**

**这个结论可能和你的直觉相反**，所以单独说清楚：
你要的"简练"由 **A + B + C** 兑现（删冗余 + 拆大文件 + 内聚职责），
**不需要动继承制**。而 a2 的**论证确实有漏洞**（1 节），
漏洞要修（改成判据式表述），但**修完之后结论不变**。

### 7.2 如果你仍希望 `BaseTool` 改 Protocol

那是**另一个决策**，需要一并处理：

1. 门槛 G22 删除（没有"实例化抛错"可测）
2. 门槛 G23 改为 `@runtime_checkable` + `isinstance`（且**签名不查**）
3. `ToolRegistry.register(tool: BaseTool)` 的类型约束**变弱**
4. `ToolDefinition.tool` 的类型从 `BaseTool` 改为 `Any`（Protocol 不能作为 Pydantic 字段类型做运行期校验）

**第 4 点影响最大**：它会打断"扩展工具与内置工具同路径注册"这条类型层约束，
而那正是架构 4.4 节的核心设计。

---

## 8. 迁移顺序（若 A/B/C 都做）

每一步都可独立验证、独立回滚。**每步结束跑三道门禁 + 两个注入实验。**

| 步 | 动作 | 验证 |
| --- | --- | --- |
| 1 | 删 `BaseLoop`，`AgentLoop` 直接实现；改写 G30 | 176 测试 + 注入 17/17 全绿 |
| 2 | 拆 `openai_compat.py` → `openai/` 包（**纯搬移，零逻辑改动**） | 同上；且 `git diff` 应只有移动 |
| 3 | 新增 `tool_calls.py`，把分片拼装从 `loop.py` 与 `provider` 收拢 | 新增单测；`loop.py` 行数应显著下降 |
| 4 | 修订 a2 表述（架构文档 + P0 骨架 6 节） | 文档一致性 |
| 5 | 跑一次真实 API 端到端确认无回归 | `scripts/real_api_agent_demo.py` |

**第 2 步的关键纪律**：拆文件必须是**纯搬移**。
一旦同时改逻辑，"测试全绿"就不能证明"重构没改变行为"——
因为测试可能同时被改了。**拆和改要分两次提交。**

---

## 9. 待确认项

**2026-09-20 本人答复：「R1 R2 R3 R4 同意，R5 就不换了」。**

| # | 问题 | 结论 |
| --- | --- | --- |
| **R1** | 子项 A/B/C 是否都做？ | ✅ **做** |
| **R2** | a2 是否按 7.1 的判据式表述修订（**不改现有实现**）？ | ✅ **是** |
| **R3** | 子项 C 里 `AssembledCall` 归属哪层？ | ✅ **放 `sigma_ai`**，接受它是个"非协议非消息"的产物类型 |
| **R4** | `tokens.py` 定位 | ✅ **写明是工具，不引入抽象** |
| **R5** | `BaseTool` 改 Protocol？ | ❌ **不换** |
| **R6** | 子项 E（Provider Registry）是否做？ | ⬜ **本次新增，待确认** |

**R5 的结论记一笔**：你选了"不换"，而代价清单（7.2 节四项）也确实不划算——
其中 `ToolDefinition.tool` 退化成 `Any` 会打断架构 4.4 节的核心设计。
**这不是妥协，是那笔交易本来就亏。**

**R5 单独说明**：这是我唯一**倾向于反对**你原始诉求的点。
不是因为"继承制更好"，而是因为换掉它会削弱三处**已经生效**的约束，
而换来的收益（少写 `(BaseTool)` 六次）与代价不成比例。
