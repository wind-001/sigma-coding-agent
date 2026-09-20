"""agent 层消息：抽象基类 + 运行时注册表 + 单向降级。

分层位置
    本模块属于 ``sigma_agent``，**是 agent 层消息的唯一归属地**。

    架构方案 4.0 节的核心结构：:

        agent 层    AgentMessage        开放联合：LLM 消息 + 应用自定义消息
                        │
                        │  convert_to_llm()      ← 单向降级，唯一通道
                        ↓
        LLM 层      LlmMessage          封闭联合：system / user / assistant / tool_result

    **这两层不是同一个东西，混为一层是本设计最容易犯的错误。**
    会话里存的东西不等于发给模型的东西。两者之间隔一个显式转换，
    于是"存了 UI 专属消息但模型看不见"是免费得到的，不需要额外机制。

``AgentMessage`` 为什么同时继承 ``BaseModel`` 和 ``ABC``
    继承 ``BaseModel`` 是为了序列化（会话树要落盘 JSONL）；
    继承 ``ABC`` 是为了强制子类实现 ``to_llm()``。

    **这是全案唯一一处「两个都要」的例外**（架构方案第 2 节第 65 行已登记）。
    ``sigma_ai`` 里刻意没有这种组合：那一层是协议对齐层，加基类只会让
    协议转换更难写（4.0.1 节）；这一层**有行为**、需要被统一持有与分派。

    **判据是"有没有行为"，不是"重不重要"。**

注册表为什么是运行期而非编译期
    Pi 用 TypeScript 的 declaration merging（编译期填充空接口），
    联合类型自动变大、运行时零开销。Python 没有这个机制。

    代价不是性能，而是**失去"忘了注册"的编译期报错**——
    所以由单测（门槛 G16）兜住。

    收益是**不用改核心包**、且可在运行时注册，与 D3 节
    「扩展层是运行时可变状态」更契合。

``role`` 为什么由注册时显式传入
    替代方案是让子类定义 ``role`` 字段再 ``getattr`` 读。
    否决理由：那样注册表与字段形成隐式耦合，且"注册时给的 role"与
    "实例上的 role 字段"可能不一致——**不一致时按哪个走？没有好答案的
    设计就是错的设计。**

本模块的依赖边界
    只依赖 ``sigma_ai`` 与 pydantic。方向是 agent → ai，单向。

异常家族（三层，别混）

    - :class:`DuplicateMessageType` —— **注册期**：role 被注册两次。
    - :class:`UnconvertibleMessageWarning` —— **降级期**：认不出的类型。
      它是一条 **warning**，不是异常（详规 2.4 方案丙）。
    - :class:`MessageDecodeError` —— **编解码期**：输入不合法或与注册表不匹配。
      :class:`UnknownMessageType` 是它的子类。

    **三者对应三个不同的时间点，处置也不同**（前两者见各自 docstring）。
    放在一起看的意义：本模块的"错误处理"不是一个策略，
    而是**按阶段分的三个策略**——想清楚每个阶段该崩还是该忍，
    正是这个模块的主要设计内容。
"""

from __future__ import annotations

import json
import time
import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    # 只用于注解。``from __future__ import annotations`` 让注解不参与求值，
    # 于是本模块与 ``types`` 之间**没有运行期依赖边**（``types`` 反向引用
    # ``AgentMessage`` 同样是 TYPE_CHECKING）。**这是解开循环导入的关键**：
    # 两个模块互相需要对方的类型，但都只在注解里用。
    from sigma_agent.types import ToolResult

# 注意：``LlmMessage`` **必须**在运行期可求值，不能只放在 TYPE_CHECKING 里。
#
# 原因：它是 ``LlmMessageWrapper.message`` 的字段类型，而 Pydantic
# 在**类定义时**就要解析注解来建校验器。放进 ``TYPE_CHECKING`` 会得到
# ``PydanticUserError: LlmMessageWrapper is not fully defined``。
#
# 这条与"减少运行期导入"的常规优化冲突，但对 ``BaseModel`` 的字段类型，
# 运行期可求值是硬要求。（这与批次 1 踩过的
# 「签名不能只被一个实现者适配」是同一类问题：约束的成立条件
# 由**框架**决定，不由写代码的人的直觉决定。）
from sigma_ai.messages import (
    ContentBlock,
    LlmMessage,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

# ---------------------------------------------------------------------------
# 异常与警告
# ---------------------------------------------------------------------------


class DuplicateMessageType(ValueError):
    """同一个 ``role`` 被注册了两次。

    **不允许静默覆盖。** 静默覆盖的后果：扩展 B 注册的类型悄悄顶掉
    扩展 A 的，而 A 的代码仍然在构造自己的类型——于是落到盘上的是
    一个"role 对但字段不对"的消息，反序列化时才炸。

    继承 ``ValueError`` 而不是自定义 ``Exception``：它确实是"值非法"，
    且走标准异常处理路径。（与批次 1 的 ``TRY004`` 经验一致：
    类型错误用 ``TypeError``，值非法用 ``ValueError``。）
    """

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(
            f"role {role!r} 已被注册。不允许静默覆盖——"
            f"若确实要替换，请先显式注销（当前不支持，说明这是设计意图外的操作）。"
        )


class MessageDecodeError(ValueError):
    """**编解码的边界异常**：输入不合法，或与注册表不匹配。

    为什么需要它，而不是直接让底层异常冒出去
        有两条不同的底层失败：

        1. ``message_to_dict`` 传进来的实例在注册表里查不到
           （没注册，或注册的是父类、传的是子类）；
        2. ``message_from_dict`` 传给 Pydantic 的数据字段不合法。

        第 2 条若让 ``pydantic.ValidationError`` 直接冒出去，
        调用方就得同时处理三种异常（本异常 / ``ValidationError`` /
        ``json.JSONDecodeError``）才能做"落盘数据坏了"这一件事。
        **调用方需要处理的异常种类数本身就是设计的一部分**——
        种类多到会漏，就等于没有错误处理。

        ``ValidationError`` 是 ``ValueError`` 的子类，所以下面用
        ``from exc`` 保留原始 traceback，排查时不会丢信息。

    **它不替换 ``convert_to_llm`` 的 warning 语义**——
    那是"运行期容错"，这是"数据不可信"，两者刻意不同。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnknownMessageType(MessageDecodeError):
    """遇到未注册的 ``role``。

    **与 ``convert_to_llm`` 的处置不同**，见 ``convert_to_llm`` 的 docstring：
    - ``convert_to_llm`` 遇未知类型 → **warning + 丢弃**（运行期容错）
    - ``message_from_dict`` 遇未知 role → **抛错**（落盘数据不可信）

    看起来矛盾，其实不矛盾：前者是"这条消息暂时没法处理"，
    后者是"这份数据坏了"。
    """

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(
            f"未知或未注册的消息 role {role!r}。已注册：{sorted(_MESSAGE_TYPES)}。"
            f"role 存在就意味着**曾经**有类型注册过它，现在没有 = "
            f"数据与代码不匹配——所以不返回 None，"
            f"静默跳过会让会话「少一条消息」而无从察觉。"
        )


class UnconvertibleMessageWarning(UserWarning):
    """``convert_to_llm`` 遇到无法降级的消息类型。

    **必须携带 ``type`` 与 ``role``**，否则 warning 本身就成了
    新的"无从排查"——这正是本警告要防的东西。
    """


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_MESSAGE_TYPES: dict[str, type[AgentMessage]] = {}


def register_message_type(
    cls: type[AgentMessage] | None = None, *, role: str
) -> Any:
    """扩展用这个函数挂载自己的消息类型。

    可直接当装饰器用::

        @register_message_type(role="custom")
        class MyMessage(AgentMessage): ...

    或显式调用::

        register_message_type(MyMessage, role="custom")

    (Pi 没有装饰器形态的能力，但这里成本为零——一个 ``return cls``。)

    重名即抛 :class:`DuplicateMessageType`，**不静默覆盖**。
    """

    def _register(target: type[AgentMessage]) -> type[AgentMessage]:
        if role in _MESSAGE_TYPES:
            raise DuplicateMessageType(role)
        _MESSAGE_TYPES[role] = target
        return target

    if cls is None:
        return _register
    return _register(cls)


def get_message_type(role: str) -> type[AgentMessage]:
    """按 role 反查类型。未注册则抛 :class:`UnknownMessageType`。"""
    try:
        return _MESSAGE_TYPES[role]
    except KeyError:
        raise UnknownMessageType(role) from None


def registered_roles() -> list[str]:
    """已注册的全部 role，排序后返回。供诊断与测试用。"""
    return sorted(_MESSAGE_TYPES)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class AgentMessage(BaseModel, ABC):
    """agent 层消息抽象基类。

    继承 ``BaseModel`` 是为了序列化（会话树要落盘 JSONL）；
    继承 ``ABC`` 是为了强制子类实现 ``to_llm()``。

    **多继承下 ``@abstractmethod`` 仍然在实例化时报错**，不需要任何
    Pydantic 配置（不要开 ``arbitrary_types_allowed``——那与本类无关）。
    由单测（门槛 G15）断言。
    """

    timestamp: int

    @abstractmethod
    def to_llm(self) -> LlmMessage | None:
        """降级成 LLM 层消息。

        返回 ``None`` 表示这条消息**不进模型上下文**
        （例如 UI 专属通知、被 ``exclude_from_context`` 标记的 bash 记录）。
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# P1 落地的两个子类（架构方案 4.0.3 节：压缩摘要留到 P2）
# ---------------------------------------------------------------------------


@register_message_type(role="llm")
class LlmMessageWrapper(AgentMessage):
    """包住四个 LLM 层消息模型。

    为什么需要 wrapper，而不是让四个 LLM 模型直接继承 ``AgentMessage``
    若那样做，LLM 层就会产生基类依赖，而架构方案 4.0.1 节的结论是
    「这一层不做抽象，加基类只会让协议转换更难写」。
    LLM 层是**协议对齐层**，它不该知道 agent 层的存在——
    这条边界由 import-linter 契约强制（门槛 G18）。

    判据：**LLM 层的字段是为协议服务的，agent 层的字段是为应用服务的。**
    混成一层的症状是"为了满足某个应用需求去给协议消息加字段"。
    """

    role: Literal["llm"] = "llm"
    message: LlmMessage

    def to_llm(self) -> LlmMessage | None:
        """规则 1：**原样透传，不重新构造。**

        这一行是整个降级函数里最需要写对的地方。反面写法::

            return AssistantMessage(role=m.role, content=m.content, ...)

        它**完全合法、能跑通**，但会在 LLM 层新增字段时静默丢字段，
        而症状（多轮对话异常）不指向根因。

        单测断言 ``result is original``（同一对象，不是值相等）——
        ``==`` 在"重建了但字段凑巧全对"时也会过。
        """
        return self.message


@register_message_type(role="tool_result")
class ToolResultAgentMessage(AgentMessage):
    """工具执行结果（agent 层）。

    与 LLM 层 ``ToolResultMessage`` 的区别是多两个字段：
    ``exclude_from_context`` 与 ``exclude_reason``。

    这两个是 **agent 层的语义**——wire protocol 里没有它们的位置
    （给 LLM 层加这个字段的话，它就必须在任何发送路径上被记得排除，
    那是典型的"靠约定维持的正确性"，会漏）。

    **判定依据**：只有 agent 层能持有"不进上下文"的语义。
    """

    role: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    content: list[ContentBlock]
    # 不进上下文，只给 UI / 审计 / 评测。判据：不需要模型看到的内容都放这里。
    details: dict[str, Any] = {}
    is_error: bool = False
    exclude_from_context: bool = False
    exclude_reason: str = ""

    @classmethod
    def from_result(
        cls,
        call: ToolCallBlock,
        result: ToolResult,
        *,
        exclude_from_context: bool = False,
        exclude_reason: str = "",
        timestamp: int | None = None,
    ) -> ToolResultAgentMessage:
        """由「工具调用 + 执行结果」构造一条 agent 层消息。

        **本方法兑现批次 1.5 详规第 9 节的 S1 项**——当时 ``ToolResult``
        类型还不存在（属批次 2），所以只登记、不实现。现在补上。

        ``exclude_from_context`` 为什么是**关键字参数**、而不是从 ``result`` 读：

        该字段是 **agent 层的语义**（wire protocol 里没有它的位置），
        而 ``ToolResult`` 是**工具层**的返回值——
        **工具不该知道"这条消息会不会进上下文"**，那是调用方（loop 或扩展）的决定。
        这与批次 1.5 的 W4 是同一条判据的延伸：**不上提到基类，就地下放到需要它的地方**。

        ``timestamp`` 可以显式传入，是为了让**回放测试确定**：
        否则两次回放的消息时间戳不同，"逐字节一致"的断言永远过不了。
        """
        return cls(
            tool_call_id=call.id,
            tool_name=call.name,
            content=result.content,
            details=result.details,
            is_error=result.is_error,
            exclude_from_context=exclude_from_context,
            exclude_reason=exclude_reason,
            timestamp=timestamp if timestamp is not None else int(time.time()),
        )

    def to_llm(self) -> LlmMessage | None:
        """规则 3：``exclude_from_context`` 的消息**显式丢弃**（返回 ``None``）。

        注意返回 ``None`` 是"这条不进上下文"，**不是**"变成一个空消息"。
        单测断言它完全不出现在结果列表里。

        ``exclude_reason`` 不参与降级——它只用于审计与 UI。
        """
        if self.exclude_from_context:
            return None
        return ToolResultMessage(
            tool_call_id=self.tool_call_id,
            tool_name=self.tool_name,
            content=self.content,
            details=self.details,
            is_error=self.is_error,
            timestamp=self.timestamp,
        )


# ---------------------------------------------------------------------------
# convert_to_llm：唯一通道
# ---------------------------------------------------------------------------


def convert_to_llm(messages: list[AgentMessage]) -> list[LlmMessage]:
    """agent 层 → LLM 层。**唯一通道。**

    为什么是自由函数而不是 ``Context`` 的方法（架构方案 4.0.5 节）
        它有**三个调用方**——常规请求、压缩摘要生成、扩展自定义。
        做成 ``Context`` 的方法会让后两者被迫构造一个假 Context。

    四条规则（对应 Pi 的 ``convertToLlm``，逐条落地）:

    1. **LLM 消息原样透传**，不重新构造（避免丢 ``*_signature`` 类字段）。
       ``LlmMessageWrapper.to_llm()`` 直接返回内部对象，不做任何包装；
       单测断言 ``is`` 同一对象。
    2. **agent 层自定义消息统一降级成 ``user`` 消息**，不新造协议 role。
       **P1 没有实际的适用对象**（唯一非 LLM 消息是工具结果，
       它降级成 ``tool_result``）；但降级路径要通——
       单测用一个临时注册的类型验证它降级成 ``user``。
       **这条不能等到 P2 再加**：压缩摘要必须降级成 ``user``（不是 ``system``），
       而那是架构方案第 8 节门禁「摘要降级位置」的断言对象。
    3. **``exclude_from_context`` 的消息显式丢弃**（``to_llm()`` 返回 ``None``）。
    4. **认不出的类型记录 warning，不静默丢弃。**

    所有消息都经 ``to_llm()``——包括自定义类型
        这是本函数唯一正确的分派方式，原因：``to_llm()`` 是
        ``AgentMessage`` 的**抽象方法**，它的存在意义就是"由子类决定
        自己怎么降级"。若在 ``convert_to_llm`` 里按 isinstance/role
        硬判分支、绕过 ``to_llm()``，就等于**架空了那个抽象方法**。

        第一版实现犯过这个错：它对已注册的自定义类型直接渲染成
        user 文本，**没有调用 ``to_llm()``**。症状是"自定义类型返回
        ``None`` 想表示不进上下文，却被强行塞进上下文"——
        子类声明的意图被静默覆盖。

        正确形状：**先问 ``to_llm()``**，返回 ``None`` 就丢弃；
        不返回 ``None`` 就直接用。这意味着"是否降级"与"降级成什么"
        两个决定都在子类手里，``convert_to_llm`` 只做遍历与去 ``None``。

        **同一个错在 2026-09-20 又犯了一次，换了个形式。**
        第二版把判据写成 ``_MESSAGE_TYPES.get(role) is not type(message)``
        ——看起来是在"查注册表"，实际上**用类相等性替子类做了丢弃决定**，
        于是已注册类型的**子类实例**仍被绕过 ``to_llm()`` 直接丢弃。
        修法是把判据拆成两个（见函数体内注释）：
        只问"注册表认不认识这个 role"，把"降级成什么"完全交还子类。
        注意 ``message_to_dict`` 里的同一表达式是**对的**，不要一起改
        ——编码要严、降级要宽，两者语义本来就相反。

        这条修正是"写得像在查注册表"掩盖了"实际在绕过抽象方法"
        的一个典型：**条件表达式读起来是对的，不等于它的语义是对的。**

    第 4 条是本模块唯一一处「不照抄 Pi」

        Pi 的做法是 ``default → undefined → filter`` **静默丢弃**。
        它敢这么做，是因为 TypeScript 的联合类型在**编译期**已封闭，
        ``default`` 分支理论上不可达，所以运行时的静默是安全的兜底。

        **Python 没有编译期穷尽性检查**——静默丢弃会变成"消息莫名消失"
        且无从排查。

        架构方案 4.0.5 节给了「记录 warning **或**直接抛错」两个选项，
        本实现选 **warning + 丢弃**：
        - 抛错的代价过高：扩展写错一个类型会让整个 session 崩掉，
          而那条消息可能只是 UI 通知；
        - 把内容塞成 user 消息会污染上下文与 prompt cache，且不可预测；
        - warning 保证"不会无从排查"——这是第 4 条真正要防的东西。

        **warning 必须带 ``type`` 与 ``role``**，否则它自己就成了新的
        "无从排查"。由单测（门槛 G14）断言。

    与 ``message_from_dict`` 的处置为什么不同
        见 :class:`UnknownMessageType` 的 docstring。
    """
    result: list[LlmMessage] = []

    for message in messages:
        # ------------------------------------------------------------------
        # 判据一：注册表里有没有这个 role？——**只问"认不认识"**
        #
        # 为什么不是 isinstance：这里要处理的正是**本模块不认识**的类型
        # （扩展注册的），而 isinstance 只能枚举已知类。
        # 这正是注册表存在的意义。
        # ------------------------------------------------------------------
        role = getattr(message, "role", None)
        if role is None or _MESSAGE_TYPES.get(role) is None:
            _warn_unconvertible(message, role)
            continue

        # ------------------------------------------------------------------
        # 判据二：降级成什么，交给实例自己的 to_llm()。
        #
        # **不检查 `type(message)` 是否恰好等于注册的那个类。**
        #
        # 2026-09-20 修（原实现是缺陷）
        #     原实现把判据写成 `_MESSAGE_TYPES.get(role) is not type(message)`，
        #     于是一个**已注册类型的子类实例**会被判成"未知类型"而丢弃——
        #     `to_llm()` 一次都没被调用过。实测：
        #
        #         class Sub(LlmMessageWrapper): pass
        #         s = Sub(timestamp=1, message=UserMessage(...))
        #         s.to_llm()          -> UserMessage(...)   # 有合法返回
        #         convert_to_llm([s]) -> []                 # 却被丢弃
        #
        #     **它同时违反了两条本模块自己写下的规则**：
        #     ① 上面「所有消息都经 to_llm()」——`is not type(...)` 就是绕过；
        #     ② 规则 3「丢弃是子类的显式表态（返回 None）」——
        #        这里却由实现替子类做了丢弃决定，**子类声明的意图被静默覆盖**。
        #
        #     注意：`message_to_dict` 里的同一表达式**是对的、不要改**。
        #     那里判的是"能不能落盘"——子类可能有父类没有的字段，
        #     存下去会被父类模型丢掉（症状是"数据缺字段"），所以必须严格要求
        #     `is not type(...)`。**同一个写法、两处语义相反，是有意的**：
        #     编码要严（宁可拒），降级要宽（由子类表态）。
        #
        #     由门槛 G21 钉住（`tests/test_agent_messages_convert.py`）。
        # ------------------------------------------------------------------
        llm = message.to_llm()
        if llm is not None:
            result.append(llm)

    return result


def render_custom_as_user(message: AgentMessage) -> UserMessage:
    """把自定义消息渲染成一条 user 消息（**辅助函数，供子类调用**）。

    这是规则 2 的可复用实现，但**它不被 ``convert_to_llm`` 直接调用**——
    要由自定义类型的 ``to_llm()`` 自己调::

        @register_message_type(role="custom")
        class CustomNote(AgentMessage):
            text: str

            def to_llm(self) -> LlmMessage | None:
                return render_custom_as_user(self)

    **为什么不做成自动的**：见 ``convert_to_llm`` docstring
    「所有消息都经 to_llm()」一段。子类必须显式表态，
    否则"不降级"与"降级"两种情况都无法区分。

    用 ``model_dump_json`` 而不是 ``str(message)``：
    后者是 Pydantic 的 repr（带类名与缩进），而 JSON 更紧凑、
    与落盘格式一致，便于模型理解结构。
    """
    return UserMessage(
        content=message.model_dump_json(exclude={"role"}),
        timestamp=message.timestamp,
    )


def _warn_unconvertible(message: AgentMessage, role: Any) -> None:
    """规则 4 的落地：记录一条**带上下文**的 warning。

    ``type`` 与 ``role`` 都要出现——只说"遇到无法降级的消息"等于没说，
    而 warning 的全部价值就在于让"消息莫名消失"变得可排查。
    """
    warnings.warn(
        f"convert_to_llm 遇到无法降级的消息："
        f"type={type(message).__name__!r}，role={role!r}。"
        f"该消息被**丢弃**（不静默——这是与 Pi 的有意差异，"
        f"见 convert_to_llm docstring 规则 4）。"
        f"已注册的 role：{registered_roles()}",
        UnconvertibleMessageWarning,
        stacklevel=3,
    )


# ---------------------------------------------------------------------------
# 编解码：注册表驱动分派（会话落盘用）
# ---------------------------------------------------------------------------


def message_to_dict(message: AgentMessage) -> dict[str, Any]:
    """把 agent 层消息序列化成可落盘的 dict。

    ``role`` 取自实例上的 ``role`` 字段（**它就是注册时传入的那个值**），
    写入 payload 以保证落盘数据里有 role 可供反查。

    **判定用"这个字段值查注册表查得到、且查到的类恰好是这个类"**
        ``getattr(message, "role", None)`` → ``_MESSAGE_TYPES.get(role)``
        → ``is not type(message)``。

        - 实例没有 ``role`` 字段 → 抛错；
        - ``role`` 值不在注册表里（没注册）→ 抛错；
        - ``role`` 值查到了类，但**不是这个类本身**（传的是子类实例）→ 抛错。

        **最后一条为什么必须用 `is not` 而不是 `isinstance`**：
        子类可能有父类没有的字段，那些字段**存不下去**
        （反序列化时按父类模型校验，多出的字段被丢）。
        症状是"数据缺字段"，离根因很远。用 `is not` 在**编码时**就暴露。

    2026-09-20 修正
        第一版实现用 **实例字段的值** 去查注册表，而
        ``_Mismatched`` 这类"实例字段默认值与注册 role 不一致"
        的类型会让它抛错（`test_role_is_written_from_registry_not_instance`
        测出来的）。现在的判定不做"值一致"的要求——
        **规范里已经要求一致，代码不该为一个违规输入做额外防御**（YAGNI）。
        注册表仍是反序列化时**唯一的事实来源**，这一点由
        ``message_from_dict`` 保证。
    """
    role = getattr(message, "role", None)
    if not isinstance(role, str) or _MESSAGE_TYPES.get(role) is not type(message):
        raise UnknownMessageType(str(role))

    payload = message.model_dump()
    payload["role"] = role
    return payload


def message_from_dict(payload: dict[str, Any]) -> AgentMessage:
    """从 dict 还原 agent 层消息。**未知 role 抛错，不返回 ``None``。**

    为什么走注册表分派而不是 Pydantic 的 discriminated union
        ``Annotated[Union[...], Field(discriminator="role")]`` 是一个
        **静态集合**：每加一个消息类型都要改那个 Union。
        而 D3 节要求「扩展层是运行时可变状态」——
        扩展注册的新类型将**无法被反序列化**，除非它能改核心包的 Union。

        **这与 D3 直接冲突**，所以只能走注册表分派。
        代价是自己写编解码（约 20 行），收益是扩展注册的类型自动生效。

    字段不合法时也抛 :class:`UnknownMessageType` 的基类 :class:`MessageDecodeError`
        **不让 ``pydantic.ValidationError`` 裸抛**。理由见
        :class:`MessageDecodeError` 的 docstring：调用方要做的事只有一件
        （"这份数据坏了"），不该为此处理三种异常。

    2026-09-20 修正
        第一版实现是 ``cls.model_validate(payload)`` 裸调用，
        于是坏数据会让 ``ValidationError`` 直接冒出去——
        这正是 ``test_broken_json_line_raises_with_line_number``
        测出来的问题。**它是真实缺陷，不是测试写错。**
    """
    role = payload.get("role")
    if not isinstance(role, str):
        raise UnknownMessageType(repr(role))

    cls = get_message_type(role)
    try:
        return cls.model_validate(payload)
    except ValidationError as exc:
        raise MessageDecodeError(
            f"role {role!r} 的数据不合法（已按 {cls.__name__} 校验）：{exc}"
        ) from exc


def messages_to_jsonl(messages: list[AgentMessage]) -> str:
    """把一组消息编成 JSONL 文本。

    **只是编解码**——不做文件 IO。JSONL 的追加写属于会话存储层
    （`sigma_session`，P1 批次 4）。这里提供它的纯函数形态，
    让它在没有存储层时也能被测试。
    """
    return "\n".join(
        json.dumps(message_to_dict(m), ensure_ascii=False) for m in messages
    )


def messages_from_jsonl(text: str) -> list[AgentMessage]:
    """从 JSONL 文本还原一组消息。空行被跳过。

    两类坏数据都抛 :class:`MessageDecodeError` 家族，且都带**行号**——
    长 JSONL 里没有行号就无从定位：

    - 该行不是合法 JSON；
    - 该行是合法 JSON 但 role 未知或字段不合法。

    **空行跳过但坏行不跳过**：前者是格式要求（追加写会在末尾留换行），
    后者是"落盘数据的问题必须早暴露"。这两件事只差一行代码，
    所以要在这里写清楚。

    2026-09-20 修正
        第一版把两类异常分别抛成 ``ValueError`` 与 ``UnknownMessageType``，
        且**都不带行号**。现在统一成 ``MessageDecodeError`` 家族
        （``UnknownMessageType`` 是它的子类，满足 ``pytest.raises`` 的老断言），
        并都带行号。**带行号是本函数存在的主要价值**——
        否则调用方拿到的异常与直接调 ``message_from_dict`` 没有区别。
    """
    result: list[AgentMessage] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise MessageDecodeError(f"第 {line_no} 行不是合法 JSON：{exc}") from exc
        try:
            result.append(message_from_dict(payload))
        except MessageDecodeError as exc:
            raise type(exc)(f"第 {line_no} 行：{exc}") from exc
    return result
