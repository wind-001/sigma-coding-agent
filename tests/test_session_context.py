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


# ---------------------------------------------------------------------------
# P2-3：项目说明进常驻区
# ---------------------------------------------------------------------------


def test_project_instructions_participate_in_fingerprint() -> None:
    """**AGENTS.md 也是常驻区**——改它同样必须抛（D4 把"项目说明"列进去了）。

    这条容易漏：只想着"系统提示词不能变"，于是 AGENTS.md 被当成
    每轮重新读进来的普通资源——而它每轮变一次就会把 prompt cache 全部打掉。
    """
    context = SessionContext(
        system_prompt="s",
        tools_schema=[],
        clock=CLOCK,
        project_instructions="原始项目约定",
    )
    assert context.build_messages()

    context._project_instructions = "改过的项目约定"

    with pytest.raises(ResidentRegionChanged):
        context.build_messages()


def test_absent_instructions_are_byte_identical_to_the_old_behaviour() -> None:
    """没有 AGENTS.md 时，常驻区**逐字节等于**加这个功能之前。

    判据与批次 7 的 ``default_registry()`` 一致：新功能不该改变既有路径的字节，
    否则"没配 AGENTS.md 的机器"会平白付一次 prompt cache 失效。
    """
    without = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    empty = SessionContext(
        system_prompt="s", tools_schema=[], clock=CLOCK, project_instructions=""
    )

    assert without.fingerprint == empty.fingerprint
    assert without.build_messages()[0].message.content == "s"  # type: ignore[union-attr]


def test_instructions_are_appended_to_the_system_message() -> None:
    """项目说明拼进 system 消息，且**排在提示词之后**。

    顺序有意：先"你是谁、有什么工具"，再"这个项目的额外约定"——
    反过来会让项目约定读起来像是工具说明的一部分。
    """
    context = SessionContext(
        system_prompt="基底提示词",
        tools_schema=[],
        clock=CLOCK,
        project_instructions="项目约定",
    )
    content = context.build_messages()[0].message.content  # type: ignore[union-attr]

    assert content.startswith("基底提示词")
    assert content.endswith("项目约定")


# ---------------------------------------------------------------------------
# P2-3：常驻区预算（门槛 G59）
# ---------------------------------------------------------------------------


def test_resident_budget_exceeded_raises() -> None:
    """**G59 本体**：超预算必须抛。

    与 G26 同样的处境——正常路径上不会触发（``resources.py`` 已把
    AGENTS.md 截到 800），所以**必须靠测试证明它会红**，
    否则它迟早被当成死代码删掉。

    超限的症状是**变慢变贵且不报错**，所以这里宁可崩。
    """
    from sigma_session.context import ResidentBudgetExceeded

    context = SessionContext(
        system_prompt="s" * 100,
        tools_schema=[],
        clock=CLOCK,
        project_instructions="x" * 100,
        resident_budget_tokens=10,  # 故意配一个必然超的上限
    )

    with pytest.raises(ResidentBudgetExceeded) as info:
        context.build_messages()

    # 异常里要能看到"用了多少 / 上限多少"——否则排查要自己重算一遍
    assert "10" in str(info.value)
    assert "超过上限" in str(info.value)


def test_resident_budget_within_limit_passes() -> None:
    """对照：不超就正常组装。**没有这条，"恒抛"也能让上面那条通过。**"""
    context = SessionContext(
        system_prompt="s",
        tools_schema=[],
        clock=CLOCK,
        project_instructions="p",
        resident_budget_tokens=10_000,
    )
    assert context.build_messages()


def test_default_budget_matches_d4() -> None:
    """默认上限就是 D4 的 3 500——**不要为了通过而偷偷放宽**。"""
    from sigma_session.context import DEFAULT_RESIDENT_BUDGET_TOKENS

    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    assert DEFAULT_RESIDENT_BUDGET_TOKENS == 3500
    assert context.resident_budget_tokens == 3500


def test_resident_tokens_counts_instructions_and_schema() -> None:
    """预算口径必须**同时**覆盖文本与工具 schema。

    只算文本的话，"加一个工具"就完全不受预算约束——
    而工具 schema 恰恰是常驻区里占比最大的那块（实测 1 018 / 1 729）。
    """
    small = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    with_schema = SessionContext(
        system_prompt="s",
        tools_schema=[{"function": {"name": "read", "description": "读文件" * 20}}],
        clock=CLOCK,
    )
    with_text = SessionContext(
        system_prompt="s" * 300, tools_schema=[], clock=CLOCK
    )

    assert with_schema.resident_tokens > small.resident_tokens
    assert with_text.resident_tokens > small.resident_tokens


# ---------------------------------------------------------------------------
# P2-3：历史改由会话树承载
# ---------------------------------------------------------------------------


def test_history_goes_through_a_tree() -> None:
    """``append`` / ``history`` 现在走树——**分支与回滚因此透传到本层**。

    验证方式是"在树上分叉、回滚，看 ``history()`` 给出哪条分支"。
    若本层还维护着一份自己的线性列表，这个测试会失败
    （列表不知道分支，只会给出全部消息）。

    **``append_to`` 会让 head 落到新节点上**（与 ``git checkout`` + ``commit``
    同义：你在那条支上继续写，那条支就是当前支）。
    要回到原支必须显式 ``set_head``——**这不是副作用，是语义**。
    """
    from sigma_agent.agent_messages import LlmMessageWrapper
    from sigma_ai.messages import UserMessage

    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)

    def _say(text: str) -> LlmMessageWrapper:
        return LlmMessageWrapper(
            timestamp=CLOCK(), message=UserMessage(content=text, timestamp=CLOCK())
        )

    def _texts() -> list[str]:
        return [m.message.content for m in context.history()]  # type: ignore[union-attr]

    context.append(_say("root"))
    common = context.tree.append(_say("common"))
    left = context.append(_say("left"))  # append 返回新节点 id
    assert _texts() == ["root", "common", "left"]

    # 从 common 分叉出右支：head 随之移到 right
    right = context.tree.append_to(_say("right"), common)
    assert context.tree.head_id == right
    assert _texts() == ["root", "common", "right"]
    assert "left" not in _texts()

    # 回滚到左支：历史跟着换回来
    context.tree.set_head(left)
    assert _texts() == ["root", "common", "left"]
    assert "right" not in _texts()
