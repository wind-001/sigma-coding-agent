"""门槛 G11 / G12 / G13 / G14：`convert_to_llm` 的四条规则逐条证伪。

**为什么这四条规则值得一个独立的测试文件**

    `convert_to_llm` 是本设计里**唯一一个"两层之间的通道"**。
    它的每条规则都对应一个具体的失败模式，而其中三条的失败
    **是完全合法的代码**——`mypy` 不报，批次 1 的
    序列化测试（G2）也拦不住：

    | 规则 | 写错的形态 | 症状 |
    | --- | --- | --- |
    | 1 原样透传 | `AssistantMessage(role=..., content=...)` 重新构造 | 多轮对话异常，根因不指向这里 |
    | 2 降级成 user | 造 `SystemMessage` | prompt cache 从插入点起全部失效 |
    | 3 显式丢弃 | 返回空消息而不是 `None` | 上下文里多一条空消息，模型行为不可预测 |
    | 4 不静默丢弃 | `continue` | 消息莫名消失且无从排查 |

    所以每条规则都要有一个**能证伪它的断言**，而不只是"跑通不报错"。

**规则 4 的断言为什么是 warning 而不是异常**

    详规 2.4 给了甲/乙/丙三个方案，选的是**丙（warning + 丢弃）**。
    所以这里断言的是"发了带上下文的 warning"，
    **不是**"抛了异常"。**断言形式必须与设计选择一致**——
    否则测试绿了，但它证明的是另一件事。

**这个文件里的临时注册为什么要加个序号**

    注册表是**模块级全局状态**，`DuplicateMessageType` 一旦触发
    就是整轮测试失败——而各测试文件之间不隔离，加序号是为了
    让将来即使有人复制粘贴整段注册代码也不冲突。
    详见 `test_agent_messages_registry.py` 的同名说明。

对应 ``docs/plans/P1-批次1.5-详规.md`` 第 5 节门槛 G11–G14，
以及 6.1 节注入清单 E11–E14（那些注入就是本文件的证伪对象）。
"""

from __future__ import annotations

import warnings

import pytest
from sigma_agent.agent_messages import (
    AgentMessage,
    LlmMessageWrapper,
    ToolResultAgentMessage,
    UnconvertibleMessageWarning,
    UnknownMessageType,
    convert_to_llm,
    register_message_type,
    render_custom_as_user,
)
from sigma_ai.messages import (
    AssistantMessage,
    LlmMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _assistant_with_all_signatures() -> AssistantMessage:
    """一条"把所有可选字段都填满"的 assistant 消息。

    **为什么不能省掉这些字段**：`*_signature` / `api` / `provider` /
    `model` / `response_id` / `error_message` 全部是**可选字段**。
    构造一条不带签名的消息，然后断言"签名没丢"——那是
    **永远成立的空断言**，注入实验（E11）也照样绿。

    这条消息的形状是由规则 1 要防的失败模式决定的：
    "重新构造"会漏掉**恰好那些不显眼的字段**，所以测试必须
    把它们全部填上。
    """
    return AssistantMessage(
        content=[
            TextBlock(text="正文", text_signature="sig-text"),
            ThinkingBlock(thinking="思考", thinking_signature="sig-thinking"),
            ToolCallBlock(
                id="call-1",
                name="read_file",
                arguments={"path": "a.txt"},
                thought_signature="sig-thought",
            ),
        ],
        api="openai-completions",
        provider="deepseek",
        model="deepseek-chat",
        response_id="resp-42",
        usage={"prompt_tokens": 7, "completion_tokens": 3, "cached_tokens": 5},  # type: ignore[arg-type]
        stop_reason="tool_use",
        error_message="",
        timestamp=1000,
    )


def _wrapped(message: LlmMessage) -> LlmMessageWrapper:
    return LlmMessageWrapper(timestamp=1000, message=message)


# ---------------------------------------------------------------------------
# G11：规则 1 —— LLM 消息原样透传（`is` 同一对象）
# ---------------------------------------------------------------------------


def test_llm_message_passes_through_as_same_object() -> None:
    """G11 本体：降级后**必须是同一个对象**（`is`），不是值相等。

    为什么必须是 `is` 而不是 `==`
        这正是 G11 与批次 1 的 G2 的分工：

        - G2 防的是**序列化丢字段**（Pydantic 自己的行为）；
        - G11 防的是**降级时重新构造**（我们自己的代码）。

        `==` 在"重建了但字段凑巧全对"时会过——而那种实现
        会在 LLM 层**新增**字段后的下一次才出问题。
        `is` 能立刻把它证伪。

    注入 E11 会把 `to_llm` 改成重新构造 `AssistantMessage`，
    本断言会红。
    """
    original = _assistant_with_all_signatures()
    wrapper = _wrapped(original)

    result = convert_to_llm([wrapper])

    assert len(result) == 1
    assert result[0] is original, (
        "降级发生了重新构造。这会在 LLM 层新增字段时静默丢字段，"
        "而症状（多轮对话异常）不指向根因。见 convert_to_llm docstring 规则 1。"
    )


def test_signatures_survive_downgrade_byte_for_byte() -> None:
    """G11 的可观测面：三个签名字段经降级后逐字节不变。

    `is` 断言已经足够证明"没重新构造"，但它是**机制层面**的断言。
    这条补的是**结果层面**的：即使将来有人把 `is` 断言改成
    `==`（错误地放宽），签名字段这一层仍然被钉住。

    填的是**非空签名**——空签名的断言是永远成立的空断言。
    """
    original = _assistant_with_all_signatures()
    result = convert_to_llm([_wrapped(original)])

    assert len(result) == 1
    downgraded = result[0]
    assert isinstance(downgraded, AssistantMessage)

    text_block, thinking_block, tool_call_block = downgraded.content
    assert isinstance(text_block, TextBlock)
    assert isinstance(thinking_block, ThinkingBlock)
    assert isinstance(tool_call_block, ToolCallBlock)
    assert text_block.text_signature == "sig-text"
    assert thinking_block.thinking_signature == "sig-thinking"
    assert tool_call_block.thought_signature == "sig-thought"


def test_provider_metadata_survives_downgrade() -> None:
    """G11 的第二个可观测面：四个 provider 元数据字段全部存活。

    这四个字段是 E11 那种"重新构造"实现**最容易漏掉**的——
    因为它们既不是内容也不是时序，看起来"不重要"。
    但它们是回放与审计的唯一依据。
    """
    original = _assistant_with_all_signatures()
    result = convert_to_llm([_wrapped(original)])

    assert len(result) == 1
    downgraded = result[0]
    assert isinstance(downgraded, AssistantMessage)
    assert downgraded.api == "openai-completions"
    assert downgraded.provider == "deepseek"
    assert downgraded.model == "deepseek-chat"
    assert downgraded.response_id == "resp-42"


@pytest.mark.parametrize(
    "message",
    [
        SystemMessage(content="系统提示词", timestamp=1),
        UserMessage(content="用户输入", timestamp=1),
        ToolResultMessage(
            tool_call_id="call-1",
            tool_name="read_file",
            content=[TextBlock(text="文件内容")],
            timestamp=1,
        ),
    ],
    ids=["system", "user", "tool_result"],
)
def test_all_four_llm_message_types_pass_through(message: LlmMessage) -> None:
    """四种 LLM 消息都必须走"原样透传"。

    只测 `AssistantMessage` 会留下盲区：另外三种可能在
    `to_llm` 里被分别处理（那样就出现四条分支，其中三条容易漏字段）。
    本模块的实现只有一条 `return self.message`，
    这条参数化断言把"只有一条路径"这个事实钉住。
    """
    result = convert_to_llm([_wrapped(message)])

    assert len(result) == 1
    assert result[0] is message


# ---------------------------------------------------------------------------
# G12：规则 3 —— `exclude_from_context` 显式丢弃
# ---------------------------------------------------------------------------


def _tool_result(
    *, exclude: bool, reason: str = "", is_error: bool = False
) -> ToolResultAgentMessage:
    return ToolResultAgentMessage(
        timestamp=2000,
        tool_call_id="call-9",
        tool_name="bash",
        content=[TextBlock(text="stdout...")],
        details={"exit_code": 1 if is_error else 0},
        is_error=is_error,
        exclude_from_context=exclude,
        exclude_reason=reason,
    )


def test_excluded_message_is_absent_from_result() -> None:
    """G12 本体：被标记的消息**完全不出现在**结果列表里。

    反面实现是"返回一个空消息占位"或"返回 `None` 但被 append 进去"。
    两种都会让上下文里多一条空消息——而模型对空消息的行为
    不可预测，且它会占 token 与 prompt cache 的位置。

    所以断言的是 `len(result) == 0` 而**不是** `result[0] is None`。
    注入 E12 会让 `exclude_from_context` 被忽略，本断言会红。
    """
    result = convert_to_llm([_tool_result(exclude=True, reason="bash 全量输出已在 details")])

    assert result == [], (
        "被 exclude_from_context 标记的消息进入了上下文。"
        "返回 None 的语义是「这条不进上下文」，不是「变成一条空消息」。"
    )


def test_excluded_message_does_not_displace_neighbours() -> None:
    """G12 的补充：丢弃一条**不得影响**相邻消息的数量与顺序。

    只测"单独一条被丢"会留下一个盲区：实现可能是
    "遇到 exclude 就 `break`"（丢弃后面全部）而不是 `continue`。
    这条用"前后各一条"把顺序与数量都钉住。
    """
    before = UserMessage(content="前一条", timestamp=1)
    after = UserMessage(content="后一条", timestamp=3)

    result = convert_to_llm(
        [
            _wrapped(before),
            _tool_result(exclude=True, reason="测试用"),
            _wrapped(after),
        ]
    )

    assert len(result) == 2
    assert result[0] is before
    assert result[1] is after


def test_non_excluded_tool_result_is_converted() -> None:
    """G12 的反面：**未标记**的工具结果必须正常降级。

    只测"标记的会丢"会滑向"全丢"。
    这条同时验证降级出来的类型正确（`ToolResultMessage` 而不是别的）。
    """
    result = convert_to_llm([_tool_result(exclude=False)])

    assert len(result) == 1
    converted = result[0]
    assert isinstance(converted, ToolResultMessage)
    assert converted.tool_call_id == "call-9"
    assert converted.tool_name == "bash"
    assert converted.details == {"exit_code": 0}
    assert converted.is_error is False


def test_exclude_reason_does_not_leak_into_llm_layer() -> None:
    """`exclude_reason` 不参与降级——它只用于审计与 UI。

    `exclude_reason` 是 agent 层字段，LLM 层**没有对应字段**
    （详规 4.3：只有 agent 层能持有"不进上下文"的语义）。
    这条断言看起来在测一个"不可能发生的事"，
    实际测的是"降级没有把 agent 字段硬塞进协议层"——
    若有人给 `ToolResultMessage` 加了这个字段，这里会红。
    """
    result = convert_to_llm([_tool_result(exclude=False, reason="不该出现")])

    assert len(result) == 1
    assert not hasattr(result[0], "exclude_reason")
    assert not hasattr(result[0], "exclude_from_context")


# ---------------------------------------------------------------------------
# G13：规则 2 —— 自定义消息降级成 `user`
# ---------------------------------------------------------------------------


@register_message_type(role="test_convert_note")
class _NoteMessage(AgentMessage):
    """测试用的自定义消息类型，模拟 P2 的压缩摘要。

    **它存在的意义就是"本批次没有实际适用对象但路径要通"**（详规 2.4 规则 2）。
    P2 的压缩摘要必须降级成 `user`（不是 `system`）——
    而那是架构方案第 8 节门禁「摘要降级位置」的断言对象。
    """

    role: str = "test_convert_note"
    text: str = ""

    def to_llm(self) -> LlmMessage | None:
        return render_custom_as_user(self)


def test_custom_message_degrades_to_user_not_system() -> None:
    """G13 本体：自定义消息降级后 `role == "user"`，**不是** `system`。

    为什么这条不能等到 P2
        摘要若降级成 `system`，它就进了**常驻区**——
        而常驻区在会话内必须逐字节稳定（D4），
        中途插入会让 prompt cache **从插入点起全部失效**。
        这个代价只在真实长会话里兑现，测试时看不出来，所以必须现在钉住。

    注入 E13 会把它改成造 `SystemMessage`，本断言会红。
    """
    result = convert_to_llm([_NoteMessage(timestamp=3000, text="压缩摘要正文")])

    assert len(result) == 1
    assert isinstance(result[0], UserMessage), (
        "自定义消息必须降级成 user。降级成 system 会让它进常驻区，"
        "破坏 prompt cache 的逐字节稳定性（D4）。见详规 2.4 规则 2。"
    )
    assert result[0].role == "user"


def test_custom_message_body_is_readable_json() -> None:
    """降级出来的 user 消息正文必须是**可读的 JSON**，不是 repr。

    `render_custom_as_user` 用 `model_dump_json` 而不是 `str(message)`：
    后者是 Pydantic 的 repr（带类名与缩进），既费 token 又难解析。
    这条断言正文里带**原始文本**且能 `json.loads` 回来——
    只断言"降级成了 user"会让"正文塞了个类名"这种实现混过去。
    """
    import json

    result = convert_to_llm([_NoteMessage(timestamp=3000, text="压缩摘要正文")])

    assert len(result) == 1
    payload = result[0]
    assert isinstance(payload, UserMessage)
    assert isinstance(payload.content, str)

    parsed = json.loads(payload.content)
    assert parsed["text"] == "压缩摘要正文"
    assert parsed["timestamp"] == 3000


def test_custom_message_can_decline_to_enter_context() -> None:
    """规则 2 与规则 3 的交汇：自定义类型返回 `None` 时不进上下文。

    **这条是本模块实现里一处真实缺陷的回归测试。**

    第一版 `convert_to_llm` 按 isinstance/role 硬判分支，
    对已注册的自定义类型**直接渲染成 user 文本，没调用 `to_llm()`**。
    症状是"自定义类型返回 `None` 想表示不进上下文，
    却被强行塞进上下文"——**子类声明的意图被静默覆盖**，
    那个抽象方法等于被架空了。

    正确形状是"所有消息都经 `to_llm()`"。
    """

    @register_message_type(role="test_convert_ephemeral")
    class _Ephemeral(AgentMessage):
        role: str = "test_convert_ephemeral"

        def to_llm(self) -> LlmMessage | None:
            return None  # 明确表态：不进上下文

    result = convert_to_llm([_Ephemeral(timestamp=4000)])

    assert result == [], (
        "自定义类型通过返回 None 表达「不进上下文」，"
        "但 convert_to_llm 绕过了 to_llm() 把它塞了进来——抽象方法被架空。"
    )


# ---------------------------------------------------------------------------
# G14：规则 4 —— 未知类型记 warning，不静默丢弃
# ---------------------------------------------------------------------------


def test_unknown_message_type_warns() -> None:
    """G14 本体：认不出的类型**必须发 warning**。

    Pi 的做法是静默丢弃（`default → undefined → filter`），
    它敢这么做是因为 TypeScript 的联合类型在编译期已封闭。
    **Python 没有编译期穷尽性检查**——静默丢弃会变成
    "消息莫名消失"且无从排查。

    注入 E14 会把它改成静默 `continue`，本断言会红。
    """

    class Unknown(AgentMessage):
        """**故意不注册**——这就是"扩展忘了注册"的形状。"""

        role: str = "test_convert_unregistered"
        text: str = ""

        def to_llm(self) -> LlmMessage | None:
            return UserMessage(content="不应到达这里", timestamp=1)

    with pytest.warns(UnconvertibleMessageWarning):
        result = convert_to_llm([Unknown(timestamp=5000, text="孤儿消息")])

    assert result == []


def test_warning_carries_type_and_role() -> None:
    """G14 的关键约束：warning 里必须带 `type` 与 `role`。

    **只说"遇到无法降级的消息"等于没说。** warning 的全部价值
    就在于让"消息莫名消失"变得可排查——不带这两个字段的话，
    warning 本身就成了新的"无从排查"，那这个设计就白做了。
    """

    class Orphan(AgentMessage):
        role: str = "test_convert_orphan"
        text: str = ""

        def to_llm(self) -> LlmMessage | None:
            return None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")  # 默认只报一次，测试里必须改成 always
        convert_to_llm([Orphan(timestamp=6000, text="x")])

    assert len(caught) == 1
    record = caught[0]
    assert issubclass(record.category, UnconvertibleMessageWarning)

    text = str(record.message)
    assert "Orphan" in text, f"warning 必须带类型名，实际：{text}"
    assert "test_convert_orphan" in text, f"warning 必须带 role，实际：{text}"


def test_warning_does_not_displace_other_messages() -> None:
    """G14 的补充：坏消息被丢弃时，**好消息必须照常降级**。

    规则 4 选的是"warning + 丢弃"而不是"抛错"，决策的代价
    由这条断言背书：一个坏消息不该毁掉整轮对话
    （详规 2.4 方案丙的理由）。
    若实现改成抛错，这条会红——那时它提醒的是"设计已经被改掉了"。
    """
    good = UserMessage(content="正常消息", timestamp=1)

    class Orphan(AgentMessage):
        role: str = "test_convert_orphan_2"

        def to_llm(self) -> LlmMessage | None:
            return None

    with pytest.warns(UnconvertibleMessageWarning):
        result = convert_to_llm([_wrapped(good), Orphan(timestamp=6000)])

    assert len(result) == 1
    assert result[0] is good


# ---------------------------------------------------------------------------
# G21：已注册类型的**子类实例**必须走 to_llm()，不得被当作未知类型丢弃
# ---------------------------------------------------------------------------


class _SubclassedWrapper(LlmMessageWrapper):
    """`LlmMessageWrapper` 的子类，**不额外注册 role**。

    `role` 字段由父类固定为 `"llm"`，所以它在注册表里查得到；
    但 `type(实例)` 不等于注册的那个类。
    """


def test_registered_subclass_instance_still_converts() -> None:
    """G21 本体：**子类实例必须被正常降级**，不能因类不相等而丢弃。

    这是 2026-09-20 修复的真实缺陷的回归测试。

    原判据 ``_MESSAGE_TYPES.get(role) is not type(message)`` 用**类相等性**
    替子类做了丢弃决定，于是：

        class Sub(LlmMessageWrapper): pass
        s = Sub(timestamp=1, message=UserMessage(...))
        s.to_llm()          -> UserMessage(...)   # 有合法返回
        convert_to_llm([s]) -> []                 # 却被丢弃

    **它同时违反两条本模块自己写下的规则**：
    ①「所有消息都经 `to_llm()`」——`is not type(...)` 就是绕过；
    ②「丢弃是子类的显式表态（返回 `None`）」——这里由实现替子类决定。

    为什么这条门槛重要：D3 说扩展层是"运行时可变状态、后期扩展"，
    **子类化已注册类型是最自然的扩展方式**。没有这条断言，
    缺陷的症状是"消息莫名消失，只有一条 warning"——
    正是本模块存在的意义所要防的那类问题。

    注入 E19 会把它改回 `is not type(...)`，本断言会红。
    """
    original = UserMessage(content="子类实例的内容", timestamp=7000)
    subclass_instance = _SubclassedWrapper(timestamp=7000, message=original)

    result = convert_to_llm([subclass_instance])

    assert len(result) == 1, (
        "已注册类型的子类实例被当作未知类型丢弃了。"
        "`to_llm()` 是子类表达降级意图的唯一入口，"
        "用类相等性判断等于替子类做决定。"
    )
    assert result[0] is original


def test_subclass_instance_does_not_warn() -> None:
    """G21 的配套：子类实例**不得发 warning**。

    只断言"没被丢弃"会漏一种情况：实现可能既丢弃又恰好返回了别的东西，
    或者上游把它救回来了但 warning 仍在刷。
    warning 的实际用途是"提示扩展忘了注册"——
    对一个**已注册类型**的子类发这条 warning 是假警报，
    会让真正的"忘了注册"淹没在噪声里。
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        convert_to_llm(
            [_SubclassedWrapper(timestamp=7000, message=UserMessage(content="x", timestamp=1))]
        )

    noisy = [w for w in caught if issubclass(w.category, UnconvertibleMessageWarning)]
    assert noisy == [], f"子类实例不该触发未知类型 warning，实际：{[str(w.message) for w in noisy]}"


def test_subclass_explicit_none_is_still_respected() -> None:
    """G21 与规则 3 的交汇：子类返回 `None` 时**仍然要丢弃**。

    拆判据之后"丢弃"的**唯一合法入口**是子类自己返回 `None`。
    若把"丢弃"整个关掉（误读成"什么都不能丢"），
    规则 3 就失效了——这条把两个方向同时钉住。
    """

    class _DecliningWrapper(LlmMessageWrapper):
        def to_llm(self) -> LlmMessage | None:
            return None

    result = convert_to_llm([_DecliningWrapper(timestamp=8000, message=UserMessage(content="x", timestamp=1))])

    assert result == [], "子类显式返回 None 表示不进上下文，必须被尊重"


def test_encode_stays_strict_for_subclass() -> None:
    """**编码判据不得跟着一起放宽**——两处 `is not` 语义相反是有意的。

    `convert_to_llm` 改宽（由子类表态）是对的；
    `message_to_dict` 必须保持严格：子类可能有父类没有的字段，
    存下去会被父类的模型丢掉，症状是"数据缺字段"，离根因很远。

    这条防的是"修一处顺手改另一处"——
    两个函数的判据长得一样，但一个要宽一个要严。
    """
    from sigma_agent.agent_messages import message_to_dict

    with pytest.raises(UnknownMessageType):
        message_to_dict(
            _SubclassedWrapper(timestamp=7000, message=UserMessage(content="x", timestamp=1))
        )


# ---------------------------------------------------------------------------
# 空输入与混合输入
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_and_is_quiet() -> None:
    """空输入返回空列表，且**不得发 warning**。

    这条防的是"把 `message.role` 读不到也当成未知类型"——
    空列表本身就是合法的（会话刚开始时就是空的）。
    """

    class SilentWarningCollector:
        pass

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = convert_to_llm([])

    assert result == []
    assert [w for w in caught if issubclass(w.category, UnconvertibleMessageWarning)] == []
    _ = SilentWarningCollector  # 保持 import 结构清晰，无功能含义


def test_mixed_batch_preserves_order() -> None:
    """混合输入下，输出顺序必须与输入顺序一致（丢弃项除外）。

    顺序是 agent loop 的硬约束：工具结果的顺序错位会让
    `tool_call_id` 与模型请求对不上。
    """
    first = UserMessage(content="第一条", timestamp=1)
    second = UserMessage(content="第二条", timestamp=2)
    third = UserMessage(content="第三条", timestamp=3)

    result = convert_to_llm(
        [
            _wrapped(first),
            _tool_result(exclude=True, reason="丢弃项"),
            _NoteMessage(timestamp=3000, text="自定义"),
            _wrapped(second),
            _wrapped(third),
        ]
    )

    assert len(result) == 4
    assert result[0] is first
    assert isinstance(result[1], UserMessage)  # 自定义消息降级来的
    assert result[2] is second
    assert result[3] is third
