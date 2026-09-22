"""会话压缩（``compact.py``）的门禁测试。

对应 ``docs/plans/P2-会话树与上下文-详规.md`` 第 5 节门槛 **G51 / G52**，
以及第 6 节测试计划第 9–11 条。

**这个文件里两组断言最要紧**

1. **压缩不能动常驻区**（G51）。摘要是 ``user`` 消息而不是 ``system``——
   若它进了常驻区，prompt cache 每压一次就全失效一次，而失效是静默的。
   断言方式是"压缩前后常驻区指纹逐字节不变"，比断言"role 是 user"更强：
   后者只测了类型，前者测了**后果**。

2. **摘要提示词必须含三条保真要求**（G52）。少了任何一条，
   摘要就会丢那类信息——而丢的症状是 agent **重复踩已经踩过的坑**，
   离根因很远（它看起来像"模型不够聪明"）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sigma_agent.agent_messages import (
    AgentMessage,
    LlmMessageWrapper,
    ToolResultAgentMessage,
    convert_to_llm,
)
from sigma_ai.base import NeverCancelled
from sigma_ai.fake import FakeProvider
from sigma_ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    Usage,
    UserMessage,
)
from sigma_ai.tokens import estimate_messages
from sigma_session.compact import (
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_TRIGGER_RATIO,
    SUMMARY_PROMPT,
    CompactionOutcome,
    CompactionPolicy,
    CompactionSummary,
    EmptySummary,
    compact_history,
    needs_compaction,
    render_history,
    split_for_compaction,
)
from sigma_session.context import SessionContext

from sigma_ai.stamps import from_epoch as ts
CLOCK = lambda: ts(1_700_000_000)  # noqa: E731 - 固定时钟，保证可复现

# ---------------------------------------------------------------------------
# 构造消息
# ---------------------------------------------------------------------------


def _rounds(count: int) -> list[AgentMessage]:
    """造 ``count`` 轮（每轮 = 一条 user + 一条 assistant）。"""
    messages: list[AgentMessage] = []
    for index in range(1, count + 1):
        messages.append(
            LlmMessageWrapper(
                timestamp=ts(index * 2 - 1),
                message=UserMessage(content=f"任务{index}", timestamp=ts(index * 2 - 1)),
            )
        )
        messages.append(
            LlmMessageWrapper(
                timestamp=ts(index * 2),
                message=AssistantMessage(
                    content=[TextBlock(text=f"回复{index}")],
                    timestamp=ts(index * 2),
                    provider="fake",
                    model="fake",
                    usage=Usage(prompt_tokens=1, completion_tokens=1),
                    stop_reason="stop",
                ),
            )
        )
    return messages


def _summary_round(text: str) -> list[dict[str, object]]:
    return [
        {"type": "text_delta", "text": text},
        {"type": "usage", "usage": {"prompt_tokens": 40, "completion_tokens": 12}},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _text_round(text: str) -> list[dict[str, object]]:
    return [
        {"type": "text_delta", "text": text},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _policy(**kwargs: object) -> CompactionPolicy:
    base: dict[str, object] = {"context_window_tokens": 1000}
    base.update(kwargs)
    return CompactionPolicy(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# G52：摘要提示词的三条保真要求
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requirement",
    ["任务目标", "已经改过哪些文件", "失败过什么"],
)
def test_summary_prompt_requires_each_fidelity_item(requirement: str) -> None:
    """**G52 本体**：三条保真要求逐条必须在提示词里。

    参数化而不是一次断言三条：**一次断言三条时，删掉一条会一起报错，
    读的人不知道是哪一条失效了**；分开之后红的那条直接指出缺哪项。
    这也让注入实验（删一条）能精确定位。
    """
    assert requirement in SUMMARY_PROMPT


def test_summary_prompt_forbids_inventing() -> None:
    """提示词要**明令禁止臆测**。

    "不要臆测"是摘要这类任务最容易出事的地方：模型会为了让摘要"完整"
    而补上原文没有的信息，而下游 agent 会照着一条**编造的事实**行动。
    """
    assert "不要臆测" in SUMMARY_PROMPT


def test_build_summary_prompt_includes_history() -> None:
    """提示词 + 历史要一起进去——只发提示词会让模型压一段空气。"""
    from sigma_session.compact import build_summary_prompt

    prompt = build_summary_prompt("[user] 把 a.py 改成异步")

    assert SUMMARY_PROMPT in prompt
    assert "把 a.py 改成异步" in prompt


# ---------------------------------------------------------------------------
# 触发条件（§6 第 9 条）
# ---------------------------------------------------------------------------


def test_trigger_boundary() -> None:
    """动态区**恰好到 80% 就触发**，差一点不触发。

    断言边界两侧（560 / 559）而不是断言"大概会触发"：
    阈值类逻辑的缺陷几乎都出现在边界上（`>` 写成 `>=`）。
    """
    policy = _policy(context_window_tokens=1000)  # 预算 = 1000 - 300 = 700
    resident = 300

    assert policy.dynamic_budget_tokens(resident) == 700
    assert needs_compaction(560, policy=policy, resident_tokens=resident) is True
    assert needs_compaction(559, policy=policy, resident_tokens=resident) is False


def test_trigger_ratio_is_eighty_percent() -> None:
    """默认比例就是 0.8（P2 详规 Q3）——**不为通过而偷偷放宽**。"""
    assert DEFAULT_TRIGGER_RATIO == 0.80


def test_no_trigger_when_resident_alone_exceeds_window() -> None:
    """常驻区自己就超窗口时**不触发压缩**。

    压了也没用——省下来的是历史，而超的是常驻区。
    这种配置错误应该由常驻区预算闸（G59）去报，不该让压缩背锅。
    """
    policy = _policy(context_window_tokens=500)
    assert policy.dynamic_budget_tokens(resident_tokens=900) == 0
    assert needs_compaction(10_000, policy=policy, resident_tokens=900) is False


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------


def test_split_keeps_last_n_rounds() -> None:
    """保留最近 N 轮原文，其余准备压缩。

    "一轮"的边界是 **user 消息**：一轮里模型可能调多次工具，
    按 assistant 切会把一轮切成好几段，于是"保留 3 轮"实际只保留了
    3 次模型调用——**症状是保留得比预期少**，而看起来又"像是对的"。
    """
    messages = _rounds(5)
    to_compact, kept = split_for_compaction(messages, keep_recent_rounds=3)

    assert len(kept) == 6  # 3 轮 × (user + assistant)
    assert len(to_compact) == 4
    assert kept[0].message.content == "任务3"  # type: ignore[union-attr]


def test_split_counts_tool_calls_as_part_of_one_round() -> None:
    """一轮里夹着工具调用时，**不把它当成新的一轮**。

    这是上一条的强化：真实会话里一轮往往是
    user → assistant(调工具) → tool_result → assistant(收尾)，
    按 assistant 切会把它拆开。
    """
    messages: list[AgentMessage] = [
        *_rounds(1),
        LlmMessageWrapper(
            timestamp=ts(3),
            message=UserMessage(content="任务2", timestamp=ts(3)),
        ),
        LlmMessageWrapper(
            timestamp=ts(4),
            message=AssistantMessage(
                content=[TextBlock(text="我读一下")],
                timestamp=ts(4),
                provider="fake",
                model="fake",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                stop_reason="tool_use",
            ),
        ),
        ToolResultAgentMessage(
            tool_call_id="c1",
            tool_name="read",
            content=[TextBlock(text="文件内容")],
            timestamp=ts(5),
        ),
        LlmMessageWrapper(
            timestamp=ts(6),
            message=AssistantMessage(
                content=[TextBlock(text="读完了")],
                timestamp=ts(6),
                provider="fake",
                model="fake",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                stop_reason="stop",
            ),
        ),
    ]

    to_compact, kept = split_for_compaction(messages, keep_recent_rounds=1)

    # 只保留最后 1 轮（任务2 那一整轮），前面 2 条被压
    assert len(to_compact) == 2
    assert kept[0].message.content == "任务2"  # type: ignore[union-attr]
    assert len(kept) == 4


def test_split_returns_nothing_to_compact_when_rounds_are_few() -> None:
    """轮数不够时**什么都不压**（返回空的可压段）。

    硬压会丢信息，而"丢信息"在这里是不可逆的——摘要一旦生成，
    原文就不再发给模型了。
    """
    to_compact, kept = split_for_compaction(_rounds(2), keep_recent_rounds=4)

    assert to_compact == []
    assert len(kept) == 4


@pytest.mark.parametrize("keep", [0, -1])
def test_split_with_non_positive_keep_compacts_nothing(keep: int) -> None:
    """``keep_recent_rounds <= 0`` 是配置错误，**按"不压"处理**。

    不按"全压"处理——那会把整段历史换成一条摘要，
    是这套机制里破坏力最大的一个动作，不该由一次配置笔误触发。
    """
    to_compact, kept = split_for_compaction(_rounds(3), keep_recent_rounds=keep)
    assert to_compact == []
    assert len(kept) == 6


def test_default_keep_rounds_is_documented_value() -> None:
    assert DEFAULT_KEEP_RECENT_ROUNDS == 4


# ---------------------------------------------------------------------------
# G51：摘要降级成 user，且不进常驻区
# ---------------------------------------------------------------------------


def test_summary_degrades_to_user_not_system() -> None:
    """**G51 本体（类型层）**：摘要降级成 ``user``。

    改成 ``system`` 的后果不是"类型不对"，而是**prompt cache 全失效**：
    系统消息属于常驻区（D4），而摘要在会话里会不断变化。
    """
    llm = CompactionSummary(timestamp=ts(1), summary="之前改过 a.py").to_llm()

    assert isinstance(llm, UserMessage)
    assert not isinstance(llm, SystemMessage)


def test_summary_body_says_it_is_not_a_new_request() -> None:
    """摘要正文要**开头就说清"这不是新的请求"**。

    它渲染成 ``user`` 消息，而模型天然把最后一条 user 当成当前任务——
    若摘要读起来像一段新指令，模型会去执行一件早就做完的事。
    """
    text = CompactionSummary(timestamp=ts(1), summary="改过 a.py").render()

    assert "不是新的请求" in text
    assert "改过 a.py" in text


def test_compaction_does_not_change_the_resident_region() -> None:
    """**G51 的后果层**：压缩前后常驻区指纹**逐字节不变**。

    这条比"role 是 user"强：它测的是**后果**（prompt cache 不受影响），
    而不是形式。若哪天有人把摘要挪进系统消息，这条会立刻红。
    """
    context = SessionContext(
        system_prompt="提示词", tools_schema=[], clock=CLOCK, session_id="s1"
    )
    context.append(*_rounds(6))
    full = len(context.effective_history())
    before = context.fingerprint

    head = context.tree.head_id
    assert head is not None
    keep_from = context.tree.path_to(head)[4]
    context.apply_compaction(
        CompactionSummary(timestamp=CLOCK(), summary="摘要"), keep_from=keep_from
    )

    # 先证明压缩**真的生效了**——否则下面"指纹没变"是空转通过
    # （什么都没压的时候它当然不变，而那测不出这条门槛要防的东西）。
    assert len(context.effective_history()) < full
    assert context.fingerprint == before

    # 而且摘要确实在**系统消息之后**（动态区），不是常驻区的一部分。
    # 断言走 ``convert_to_llm`` 而不是直接读字段：要证明的是
    # **"模型看到的最后一条 user 就是摘要"**，那才是这条门槛的实质。
    # （直接读 ``m.message`` 会踩坑：``build_messages()`` 返回的是 **agent 层**
    # 消息，而 ``CompactionSummary`` 上没有 ``message`` 字段。）
    built = context.build_messages()
    llm_messages = convert_to_llm(built)
    assert isinstance(llm_messages[0], SystemMessage)
    assert any(
        isinstance(m, UserMessage) and "摘要" in str(m.content)
        for m in llm_messages[1:]
    )


# ---------------------------------------------------------------------------
# 有效历史（视图）
# ---------------------------------------------------------------------------


def test_effective_history_is_summary_plus_kept_window() -> None:
    """``effective_history`` = ``[摘要, *保留窗口]``，且比原始历史短。"""
    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    context.append(*_rounds(6))

    messages = context.effective_history()
    _, kept = split_for_compaction(messages, keep_recent_rounds=2)
    keep_from = context.tree.path_to(context.tree.head_id)[  # type: ignore[arg-type]
        len(messages) - len(kept)
    ]
    context.apply_compaction(
        CompactionSummary(timestamp=CLOCK(), summary="摘要"), keep_from=keep_from
    )

    effective = context.effective_history()

    assert len(effective) < len(messages)
    assert isinstance(effective[0], CompactionSummary)
    assert len(effective) == 1 + len(kept)


def test_raw_history_is_untouched_by_compaction() -> None:
    """**压缩不改历史。** 原始历史条数一条不少。

    这是本项目对 P2 详规 Q3 的有意偏离（Q3 说"替换"）：
    树是只追加的事实记录，改写它会让审计链断掉。
    由 ``store.py`` / ``compact.py`` 两处 docstring 分别说明。
    """
    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    context.append(*_rounds(5))
    before = len(context.history())

    context.apply_compaction(
        CompactionSummary(timestamp=CLOCK(), summary="摘要"),
        keep_from=context.tree.path_to(context.tree.head_id)[0],  # type: ignore[arg-type]
    )

    assert len(context.history()) == before  # 原始历史不受影响


def test_compaction_survives_later_appends() -> None:
    """压缩之后继续追加消息，保留窗口**跟着走**。

    ``keep_from`` 存的是**节点 id** 而不是下标，正是为了这个：
    下标会在继续追加时漂移，而 id 稳定。
    这条用一个"压完再追加两轮"的场景钉住它。
    """
    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    context.append(*_rounds(5))
    head = context.tree.head_id
    assert head is not None
    keep_from = context.tree.path_to(head)[4]
    context.apply_compaction(
        CompactionSummary(timestamp=CLOCK(), summary="摘要"), keep_from=keep_from
    )
    length_before = len(context.effective_history())

    context.append(*_rounds(2))  # 再追加两轮

    after = context.effective_history()
    assert len(after) == length_before + 4  # 摘要仍在，窗口变长
    assert isinstance(after[0], CompactionSummary)


def test_clear_compaction_restores_full_history() -> None:
    """``clear_compaction`` 回到完整历史（诊断与测试用）。"""
    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    context.append(*_rounds(4))
    full = len(context.effective_history())
    head = context.tree.head_id
    assert head is not None

    context.apply_compaction(
        CompactionSummary(timestamp=CLOCK(), summary="x"),
        keep_from=context.tree.path_to(head)[4],
    )
    assert len(context.effective_history()) < full

    context.clear_compaction()
    assert len(context.effective_history()) == full
    assert context.summary is None


def test_effective_history_falls_back_when_keep_from_is_gone() -> None:
    """``keep_from`` 指向的节点不在了 → **退回完整历史**，不是只发摘要。

    只发摘要的后果最坏：模型会以为"之前什么都没发生"。
    多发几条消息只是多花 token，**丢信息是没法补的**。
    """
    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    context.append(*_rounds(3))

    context.apply_compaction(
        CompactionSummary(timestamp=CLOCK(), summary="摘要"), keep_from="不存在的节点"
    )

    assert len(context.effective_history()) == len(context.history())


# ---------------------------------------------------------------------------
# 调用（离线：FakeProvider）
# ---------------------------------------------------------------------------


async def test_compact_history_returns_none_when_nothing_to_compact() -> None:
    """没有可压的段时返回 ``None``——**不是"压了 0 条"的结果**。

    两者含义不同：``None`` 是"这次不需要压"，而"压了 0 条"如果真的出现，
    说明切分逻辑有问题。用 ``None`` 把这两件事分开。
    """
    provider = FakeProvider.from_rounds([])
    outcome = await compact_history(
        _rounds(2),
        policy=_policy(),
        provider=provider,
        model="fake",
        signal=NeverCancelled(),
        clock=CLOCK,
    )

    assert outcome is None
    assert provider.remaining_rounds == 0  # 一次都没调 provider


async def test_compact_history_produces_a_user_message() -> None:
    """端到端：压一次 → 得到一条降级成 ``user`` 的摘要消息。"""
    provider = FakeProvider.from_rounds([_summary_round("用户要改 a.py；b.sh 失败过")])
    outcome = await compact_history(
        _rounds(6),
        policy=_policy(keep_recent_rounds=1),
        provider=provider,
        model="fake",
        signal=NeverCancelled(),
        clock=CLOCK,
    )

    assert isinstance(outcome, CompactionOutcome)
    assert outcome.compacted_messages == 10
    assert outcome.kept_messages == 2
    assert isinstance(outcome.summary.to_llm(), UserMessage)
    assert provider.remaining_rounds == 0


async def test_summary_prompt_is_what_gets_sent() -> None:
    """真正发出去的是**拼好历史的提示词**，不是干提示词。

    这条防的是"提示词改了但忘了拼历史"——那种情况摘要会基于空气生成，
    而模型仍然会输出一段看起来很像摘要的文字。
    """
    provider = FakeProvider.from_rounds([_summary_round("摘要")])
    await compact_history(
        _rounds(4),
        policy=_policy(keep_recent_rounds=1),
        provider=provider,
        model="fake",
        signal=NeverCancelled(),
        clock=CLOCK,
    )

    # FakeProvider 不记录请求，所以这里直接验渲染结果
    text = render_history(_rounds(4))
    assert "[user] 任务1" in text
    assert "[assistant] 回复1" in text


async def test_compaction_cost_is_recorded_in_details() -> None:
    """**R2.4**：压缩那次调用的用量必须能被评测拿到。

    漏记的后果是"每任务 token"**系统性偏低**——长会话越省越显得划算，
    而省下来的正是没被记账的那次调用。
    """
    provider = FakeProvider.from_rounds([_summary_round("摘要")])
    outcome = await compact_history(
        _rounds(6),
        policy=_policy(keep_recent_rounds=1),
        provider=provider,
        model="fake",
        signal=NeverCancelled(),
        clock=CLOCK,
    )

    assert outcome is not None
    assert outcome.usage is not None
    assert outcome.usage.completion_tokens == 12
    assert outcome.summary.details["compaction_usage"]["prompt_tokens"] == 40
    assert outcome.summary.details["compacted_messages"] == 10


async def test_empty_summary_raises_instead_of_silently_compacting() -> None:
    """provider 没吐出任何文本 → **抛错**，不静默产出一条空摘要。

    空摘要比不压缩更坏：它会让模型以为"之前什么都没发生"，
    而这条错误信息是**不可逆**的（原文已经不再发给模型了）。
    """
    provider = FakeProvider.from_rounds(
        [[{"type": "stop", "stop_reason": "stop"}]]
    )

    with pytest.raises(EmptySummary):
        await compact_history(
            _rounds(6),
            policy=_policy(keep_recent_rounds=1),
            provider=provider,
            model="fake",
            signal=NeverCancelled(),
            clock=CLOCK,
        )


def test_render_history_goes_through_convert_to_llm() -> None:
    """渲染历史走 ``convert_to_llm``（agent → LLM 的唯一通道）。

    绕过它意味着"存了但模型看不见"的消息（如 ``exclude_from_context``）
    在摘要里突然可见——**两处口径就不一致了**。
    """
    excluded = ToolResultAgentMessage(
        tool_call_id="c1",
        tool_name="bash",
        content=[TextBlock(text="不该出现在摘要里")],
        exclude_from_context=True,
        timestamp=ts(9),
    )
    text = render_history([*_rounds(1), excluded])

    assert "任务1" in text
    assert "不该出现在摘要里" not in text


# ---------------------------------------------------------------------------
# 与 SessionContext / sdk 的接线
# ---------------------------------------------------------------------------


def test_should_compact_uses_the_policy() -> None:
    """``SessionContext.should_compact`` 把动态区与策略接起来。

    用一个**必然触发**的窗口（动态区只要大于 0 就超），
    避免这条断言依赖具体的 token 估算数值——估算口径是另一回事。
    """
    context = SessionContext(
        system_prompt="s", tools_schema=[], clock=CLOCK, resident_budget_tokens=3500
    )
    context.append(*_rounds(6))

    # 窗口必须**大于常驻区**：否则 dynamic_budget 被夹到 0，按设计不触发
    # （那是配置错误，归常驻区预算闸 G59 管）。
    tight = CompactionPolicy(
        context_window_tokens=context.resident_tokens + 1, trigger_ratio=0.0
    )
    roomy = CompactionPolicy(context_window_tokens=10_000_000)

    assert context.dynamic_tokens() > 0
    assert context.should_compact(tight) is True
    assert context.should_compact(roomy) is False


async def test_context_compact_applies_the_view() -> None:
    """``SessionContext.compact`` 端到端：切分 → 调模型 → 接到视图上。"""
    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    context.append(*_rounds(6))
    provider = FakeProvider.from_rounds([_summary_round("用户改了 a.py")])

    outcome = await context.compact(
        policy=_policy(keep_recent_rounds=1),
        provider=provider,
        model="fake",
        signal=NeverCancelled(),
    )

    assert outcome is not None
    assert context.summary is not None
    effective = context.effective_history()
    assert isinstance(effective[0], CompactionSummary)
    # 原始历史不变
    assert len(context.history()) == 12


async def test_dynamic_tokens_shrink_after_compaction() -> None:
    """压缩**确实让发给模型的那份变小了**——否则整个机制没有意义。"""
    context = SessionContext(system_prompt="s", tools_schema=[], clock=CLOCK)
    context.append(*_rounds(8))
    before = context.dynamic_tokens()
    provider = FakeProvider.from_rounds([_summary_round("简短摘要")])

    await context.compact(
        policy=_policy(keep_recent_rounds=1),
        provider=provider,
        model="fake",
        signal=NeverCancelled(),
    )

    assert context.dynamic_tokens() < before


async def test_interactive_session_compacts_automatically(tmp_path: Path) -> None:
    """``InteractiveSession.send`` 在触发时**自动**压一次。

    接线断掉的话 ``compact.py`` 就是死代码——而"写了但没接上"
    正是本项目明确要避免的失败模式。这里用真实路径跑一遍：
    第一次 send 时动态区已超阈值 → 先压（消耗 transcript 第 1 轮）
    → 再跑本轮任务（消耗第 2 轮）。
    """
    from sigma import sdk

    provider = FakeProvider.from_rounds(
        [
            _summary_round("之前改了 a.py"),  # 压缩调用
            _text_round("本轮回答"),  # loop 的调用
        ]
    )
    session = sdk.InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        # 窗口要**大于常驻区**（默认注册表约 1149）：等于 1 会让动态预算为 0，
        # 于是按设计不触发。2000 给出正的预算，配 ratio=0 必然触发。
        compaction_policy=CompactionPolicy(
            context_window_tokens=2000, trigger_ratio=0.0
        ),
    )
    # 先塞一点历史，让"最旧的一段"真的存在（否则没有可压的段）
    session.context.append(*_rounds(5))

    result = await session.send("新的任务")

    assert result.text == "本轮回答"
    assert session.last_compaction is not None
    assert session.last_compaction.summary.summary == "之前改了 a.py"
    assert provider.remaining_rounds == 0


async def test_interactive_session_does_not_compact_when_roomy(
    tmp_path: Path,
) -> None:
    """对照：窗口足够大时**不压**，provider 只被调一次（本轮任务）。

    没有这条，"永远压"也能让上一条通过。
    """
    from sigma import sdk

    provider = FakeProvider.from_rounds([_text_round("回答")])
    session = sdk.InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        compaction_policy=CompactionPolicy(context_window_tokens=10_000_000),
    )
    session.context.append(*_rounds(5))

    await session.send("任务")

    assert session.last_compaction is None
    assert provider.remaining_rounds == 0


async def test_compaction_failure_does_not_break_the_session(tmp_path: Path) -> None:
    """压缩失败（这里是"空摘要"）**不打断本轮任务**。

    压缩是"为了跑下去"的优化；它自己失败不该让用户丢掉整个会话——
    那就本末倒置了。所以异常被降级成"这一轮不压"。
    """
    from sigma import sdk

    provider = FakeProvider.from_rounds(
        [
            [{"type": "stop", "stop_reason": "stop"}],  # 压缩产出空 → 抛 EmptySummary
            _text_round("照常回答"),
        ]
    )
    session = sdk.InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        compaction_policy=CompactionPolicy(
            context_window_tokens=2000, trigger_ratio=0.0
        ),
    )
    session.context.append(*_rounds(5))

    result = await session.send("任务")

    assert result.text == "照常回答"
    assert session.last_compaction is None
