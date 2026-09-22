"""门槛 G15 / G16：`BaseModel + ABC` 多继承的实例化保护，与注册表。

**G15 为什么值得单独一个门槛**

    批次 1 分别测过两种形态：

    - 纯 `ABC`（`BaseProvider` / `CancelToken`）——`test_sigma_ai_types.py`
    - 纯 `BaseModel`（四个 LLM 消息）——同上

    **但没有测过两者多继承**。`AgentMessage` 是 `BaseModel, ABC`，
    它是一个**新的技术组合**：Pydantic 会重建类（metaclass 是
    `ModelMetaclass`），而 `ABC` 的抽象方法集合靠 `__abstractmethods__` 维护。
    两者是否互相干扰，**不能假设，只能测**。

    实测结论（本文件断言）：不干扰。`@abstractmethod` 在多继承下
    仍然在**实例化**时报错，且 `model_fields` 与 `__abstractmethods__`
    各管各的。

**G16 为什么是"注册后能反查"而不只是"重名抛错"**

    只测重名抛错会产生一个盲区：**注册成功了但查不到**。
    那会让 `message_from_dict` 在反序列化时找不到类型，
    而症状是"存下去的消息读不回来"——比注册失败更难排查。

对应 ``docs/plans/P1-批次1.5-详规.md`` 第 5 节门槛 G15 / G16。
"""

from __future__ import annotations

import pytest
from sigma_agent.agent_messages import (
    AgentMessage,
    DuplicateMessageType,
    LlmMessageWrapper,
    ToolResultAgentMessage,
    get_message_type,
    register_message_type,
    registered_roles,
)
from sigma_ai.messages import LlmMessage

# ---------------------------------------------------------------------------
# G15：BaseModel + ABC 多继承下的抽象保护
# ---------------------------------------------------------------------------


from sigma_ai.stamps import from_epoch as ts
def test_agent_message_cannot_be_instantiated() -> None:
    """G15 本体：抽象基类直接实例化抛 `TypeError`。

    这是「继承制」（a2）在多继承场景下的落地验证。
    若这条不成立，`to_llm()` 就可能被忘记实现，而后果是
    降级时静默失败——正是门槛存在的理由。
    """
    with pytest.raises(TypeError, match="abstract"):
        AgentMessage(timestamp=ts(1))  # type: ignore[abstract]


def test_half_done_subclass_still_fails() -> None:
    """G15 的补充：**半实现子类**同样不得实例化。

    只测基类不够——基类必抛，那是最容易通过的情形。
    真正要防的是"子类忘了实现 `to_llm`"。

    **构造"半实现"的正确方式**：子类只提供字段、不重写 `to_llm`。
    **错误方式**是给基类再加一个测试专用的抽象方法——那是污染生产代码，
    而测试不该有这种权力。
    """

    class HalfDone(AgentMessage):
        note: str = ""

        # 故意不实现 to_llm

    with pytest.raises(TypeError, match="abstract"):
        HalfDone(timestamp=ts(1))  # type: ignore[abstract]


def test_model_fields_and_abstractmethods_do_not_interfere() -> None:
    """G15 的机制验证：Pydantic 与 ABC 各管各的，互不干扰。

    这条断言的是**元数据**而不是行为，因为"两者是否互相干扰"这件事
    是无法从行为反推的：

    - `model_fields` 必须只有 `timestamp`（ABC 不该往里加东西）；
    - `__abstractmethods__` 必须恰好是 `{"to_llm"}`（Pydantic 不该吃掉它）。

    一旦某个 Pydantic 版本改了行为，这里会先红，
    而不用等到某个子类忘实现方法后在生产里静默出错。
    """
    assert sorted(AgentMessage.model_fields) == ["timestamp"]
    assert AgentMessage.__abstractmethods__ == frozenset({"to_llm"})


def test_implementing_subclass_instantiates_fine() -> None:
    """G15 的反面：**正常实现** `to_llm` 的子类必须能实例化。

    只测"该抛的抛了"会滑向"防一切"。
    这条保证抽象保护没有把正常子类也挡在外面。
    """

    @register_message_type(role="test_ok")
    class Ok(AgentMessage):
        role: str = "test_ok"
        text: str = ""

        def to_llm(self) -> LlmMessage | None:
            return None

    instance = Ok(timestamp=ts(1), text="hi")
    assert instance.timestamp == ts(1)
    assert instance.role == "test_ok"


# ---------------------------------------------------------------------------
# G16：注册表
# ---------------------------------------------------------------------------


def test_duplicate_role_raises() -> None:
    """G16 本体：同一个 `role` 注册两次必须抛错，**不静默覆盖**。

    静默覆盖的后果：扩展 B 的类型悄悄顶掉扩展 A 的，而 A 的代码
    仍在构造自己的类型——于是落到盘上的是"role 对但字段不对"的消息，
    反序列化时才炸，且那时已经离根因很远。
    """

    @register_message_type(role="test_dup")
    class First(AgentMessage):
        role: str = "test_dup"

        def to_llm(self) -> LlmMessage | None:
            return None

    with pytest.raises(DuplicateMessageType, match="test_dup"):

        @register_message_type(role="test_dup")
        class Second(AgentMessage):
            role: str = "test_dup"

            def to_llm(self) -> LlmMessage | None:
                return None


def test_registered_role_is_retrievable() -> None:
    """G16 的另一半：注册之后必须**能按 role 反查回类**。

    这条防的是"注册成功了但查不到"——它会让 `message_from_dict`
    找不到类型，症状是"存下去的消息读不回来"。
    """

    @register_message_type(role="test_lookup")
    class Lookup(AgentMessage):
        role: str = "test_lookup"

        def to_llm(self) -> LlmMessage | None:
            return None

    assert get_message_type("test_lookup") is Lookup
    assert "test_lookup" in registered_roles()


def test_register_works_as_decorator_and_as_call() -> None:
    """两种注册形态都必须工作：装饰器与显式调用。

    装饰器形态是本实现相对 Pi 的增量（Pi 只有编译期 merging），
    成本是一个 `return cls`。既然提供了就要测。
    """

    class Explicit(AgentMessage):
        role: str = "test_explicit"

        def to_llm(self) -> LlmMessage | None:
            return None

    returned = register_message_type(Explicit, role="test_explicit")

    assert returned is Explicit
    assert get_message_type("test_explicit") is Explicit


def test_unknown_role_raises_on_lookup() -> None:
    """反查未注册的 role 抛 `UnknownMessageType`，**不返回 `None`**。

    返回 `None` 会让调用方只能"检查 None"——而漏检查就变成静默丢弃，
    这正是本模块要避免的模式。
    """
    from sigma_agent.agent_messages import UnknownMessageType

    with pytest.raises(UnknownMessageType, match="test_nonexistent"):
        get_message_type("test_nonexistent")


def test_known_roles_are_sorted_and_stable() -> None:
    """`registered_roles()` 必须排序——它会被放进 warning 文本里。

    未排序的集合遍历顺序在不同进程间不稳定，会让 warning 文本
    无法比对，也会让日志 diff 出现无意义噪音。
    """
    roles = registered_roles()
    assert roles == sorted(roles)


def test_builtin_types_are_registered() -> None:
    """两个内置类型必须在 import 时已注册。

    它们靠 `@register_message_type` 装饰器自动注册——
    若装饰器形态失效，这里会发现（而只用 `get_message_type` 测的话，
    测试自己也得注册，就测不出"内置类型忘了注册"）。
    """
    assert get_message_type("llm") is LlmMessageWrapper
    assert get_message_type("tool_result") is ToolResultAgentMessage
