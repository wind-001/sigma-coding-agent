"""内存版会话上下文的测试。

重点不是"能不能存消息"，而是**常驻区纪律**（门槛 G26 / D4 / 架构 5.2 节）。

常驻区一旦在会话内变化，prompt cache 从变动点起全部失效——
而失效是**静默的**（只是变慢变贵，不会报错），所以必须用断言把它
从"容易违反的约定"变成"不可违反的约束"。
"""

from __future__ import annotations

import pytest

from sigma_session.context import ResidentRegionChanged, SessionContext

CLOCK = lambda: 1_700_000_000  # noqa: E731 - 固定时钟，保证可复现


def test_resident_region_change_raises() -> None:
    """门槛 G26 的主体：改掉系统提示词后 ``build_messages()`` 必须抛。

    **这条用例的意义是"证明断言真的会失败"**——
    P1 的启动路径上没有任何东西会改常驻区，所以这个断言平时不会触发；
    若它永远不红，就会被人当成死代码删掉。这条用例让它至少红一次。
    """
    context = SessionContext(system_prompt="原始提示词", tools_schema=[], clock=CLOCK)
    assert context.build_messages()  # 基线可用

    # 模拟"按任务动态调整系统提示词"——架构 5.2 节明令禁止的做法
    context._system_prompt = "改过的提示词"

    with pytest.raises(ResidentRegionChanged):
        context.build_messages()


def test_tool_schema_change_also_raises() -> None:
    """常驻区**不止**系统提示词——工具 schema 变了同样要抛。

    这条容易被漏掉：工具 schema 也进常驻区、也参与指纹。
    若只校验 system_prompt，"中途加一个工具"就会静默破坏 prompt cache。
    """
    context = SessionContext(
        system_prompt="s",
        tools_schema=[{"function": {"name": "read"}}],
        clock=CLOCK,
    )
    context.build_messages()

    context._tools_schema = [
        {"function": {"name": "read"}},
        {"function": {"name": "write"}},
    ]

    with pytest.raises(ResidentRegionChanged):
        context.build_messages()


def test_fingerprint_is_stable_across_builds() -> None:
    """**同一个**常驻区，多次 build 的指纹必须完全一致。

    这是上面两条的**反向保证**：断言不能误报。
    指纹算法用了 ``json.dumps(sort_keys=True)``，
    若哪天改成依赖 dict 键序，这条会先红。
    """
    context = SessionContext(
        system_prompt="s",
        tools_schema=[{"zebra": 1, "alpha": 2}],
        clock=CLOCK,
    )
    first = context.fingerprint
    context.build_messages()
    context.build_messages()
    assert context.fingerprint == first


def test_key_order_does_not_affect_fingerprint() -> None:
    """键序不同但内容相同的 schema，**指纹必须相同**。

    这条直接钉住 ``sort_keys=True`` 的必要性：少了它，"工具注册顺序变了"
    会变成"常驻区变了"，于是断言天天误报——而误报的断言会被关掉。
    """
    a = SessionContext(
        system_prompt="s", tools_schema=[{"a": 1, "b": 2}], clock=CLOCK
    )
    b = SessionContext(
        system_prompt="s", tools_schema=[{"b": 2, "a": 1}], clock=CLOCK
    )
    assert a.fingerprint == b.fingerprint


def test_history_preserves_append_order() -> None:
    """历史按追加顺序保留——架构 5.2 节规则二（动态内容只能尾部追加）。

    本类只暴露 ``append``，**没有给插入留口子**（插入会破坏 prompt cache）。
    """
    from sigma_agent.agent_messages import LlmMessageWrapper
    from sigma_ai.messages import SystemMessage

    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    for text in ("first", "second", "third"):
        context.append(
            LlmMessageWrapper(
                timestamp=CLOCK(), message=SystemMessage(content=text, timestamp=CLOCK())
            )
        )

    history = context.history()
    assert len(history) == 3
    assert [m.message.content for m in history] == ["first", "second", "third"]  # type: ignore[union-attr]
