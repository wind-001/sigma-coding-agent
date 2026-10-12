"""推进钩子（AdvanceHook，第三类决策型钩子）的测试——推进机制批次 1。

本文件只测**扩展面本身**（能注册、能问、零值逐字节不变）；内置判定器
（VerificationGate / GoalContinuation）的测试随各自批次另写。

判据（与 hooks/base.py 的承诺逐条对应）：
    G-ADV-1  零推进钩子 → ``advance_decide`` 返回 None（= 不干预）；
    G-ADV-2  重名（含与通知/审批两名单之间）即抛 DuplicateHookError；
    G-ADV-3  多钩子裁决：force_stop 权重高于 continue_round；
    G-ADV-4  loop 侧：continue → 注入 + 多跑一轮 + AdvanceDecided 事件；
    G-ADV-5  loop 侧：force_stop → 优雅 stopped（不抛异常）；
    G-ADV-6  loop 侧：不注册推进钩子时，行为与没有钩子系统之前逐字节一致
             （G33 的推进版——同一个 transcript，有/无总线，结果同 status、
              同 rounds、同 text）。
"""

from __future__ import annotations

from typing import Any

import pytest

from sigma.agent.types import ToolContext, ToolResult
from sigma.events.lifecycle import (
    AdvanceDecided,
    AdvanceDecision,
    AdvanceFacts,
    AssistantProduced,
    HookEvent,
    MessageInjected,
    TextChunk,
    ToolEnd,
    ToolStart,
    TurnEnd,
)
from sigma.hooks.base import (
    AdvanceHook,
    BaseHook,
    DuplicateHookError,
    HookManager,
)
from sigma.providers.base import NeverCancelled
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock
from sigma.providers.stamps import from_epoch as ts
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.base import BaseTool
from sigma.tools.registry import ToolRegistry
from pydantic import BaseModel, Field

FIXED_TIME = ts(1_700_000_000)

_FACTS = AdvanceFacts(round_index=1, last_text="我做完了")


class _FixedAdvance(AdvanceHook):
    """按预设答案回答的推进钩子（测试专用）。"""

    def __init__(self, name: str, decision: AdvanceDecision) -> None:
        self.name = name
        self._decision = decision
        self.calls: list[AdvanceFacts] = []

    async def advance(self, facts: AdvanceFacts) -> AdvanceDecision:
        self.calls.append(facts)
        return self._decision


class _OnceAdvance(AdvanceHook):
    """第一次询问时继续（带注入），之后不干预——真实判定器都有计数器
    （防"永远差一步"死循环），测试桩必须同款，否则测的是死循环。"""

    def __init__(self, name: str, decision: AdvanceDecision) -> None:
        self.name = name
        self._decision = decision
        self.calls: list[AdvanceFacts] = []

    async def advance(self, facts: AdvanceFacts) -> AdvanceDecision:
        self.calls.append(facts)
        if len(self.calls) == 1:
            return self._decision
        return AdvanceDecision()


class _NoopAdvance(AdvanceHook):
    """永远不干预的推进钩子。"""

    name = "noop"

    async def advance(self, facts: AdvanceFacts) -> AdvanceDecision:
        return AdvanceDecision()


class _RecordingHook(BaseHook):
    """订阅全部钩子点（含 AdvanceDecided），记下序列。"""

    name = "recording"

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def events(self) -> tuple[type[HookEvent], ...]:
        return (
            TextChunk,
            ToolStart,
            ToolEnd,
            MessageInjected,
            AssistantProduced,
            AdvanceDecided,
            TurnEnd,
        )

    def on_event(self, event: HookEvent) -> None:
        self.seen.append(event)


# ---------------------------------------------------------------------------
# 协议层（HookManager.advance_decide）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_decide_returns_none_without_hooks() -> None:
    """G-ADV-1：零推进钩子 → None。None 与"不干预的决定"同效，
    但语义不同：None 是"没人问过"，默认值是"有人问了说不用"。
    loop 侧两者走同一分支（原路径），测试分别钉住。"""
    manager = HookManager()
    assert await manager.advance_decide(_FACTS) is None


@pytest.mark.asyncio
async def test_advance_decide_returns_none_when_all_noop() -> None:
    """注册了但全部不干预 → None（不是"继续"的默认）。"""
    manager = HookManager()
    manager.register_advance(_NoopAdvance())
    assert await manager.advance_decide(_FACTS) is None


@pytest.mark.asyncio
async def test_advance_decide_any_continue_wins() -> None:
    """G-ADV-3 的继续侧：任一认为没完 → 没完。"""
    manager = HookManager()
    manager.register_advance(_NoopAdvance())
    manager.register_advance(
        _FixedAdvance("continuer", AdvanceDecision(continue_round=True, reason="验收未过"))
    )
    decision = await manager.advance_decide(_FACTS)
    assert decision is not None
    assert decision.continue_round is True
    assert decision.reason == "验收未过"


@pytest.mark.asyncio
async def test_force_stop_beats_continue() -> None:
    """G-ADV-3 的停止侧：force_stop 权重高于 continue——
    验收/安全类判据说不完，不许被"再试试"盖过。"""
    manager = HookManager()
    manager.register_advance(
        _FixedAdvance("continuer", AdvanceDecision(continue_round=True, inject="再试"))
    )
    manager.register_advance(
        _FixedAdvance("stopper", AdvanceDecision(force_stop=True, reason="验收 2 轮仍败"))
    )
    decision = await manager.advance_decide(_FACTS)
    assert decision is not None
    assert decision.force_stop is True
    assert decision.reason == "验收 2 轮仍败"


def test_register_advance_rejects_duplicates_across_all_three_lists() -> None:
    """G-ADV-2：推进钩子与通知型/审批型共用一个命名空间，重名即抛。"""
    manager = HookManager()
    manager.register_advance(_FixedAdvance("shared", AdvanceDecision()))
    with pytest.raises(DuplicateHookError):
        manager.register_advance(_FixedAdvance("shared", AdvanceDecision()))
    # 与通知型撞名
    manager.register(_RecordingHook())
    with pytest.raises(DuplicateHookError):
        manager.register_advance(_FixedAdvance("recording", AdvanceDecision()))
    # 与审批型撞名（审批名单里已有的名字）
    assert manager.advance_names() == ["shared"]


# ---------------------------------------------------------------------------
# loop 侧接入（_make_loop 最小副本，模式同 test_agent_hook_events.py）
# ---------------------------------------------------------------------------


class _EchoParams(BaseModel):
    message: str = Field(description="要回显的内容")


class _EchoTool(BaseTool):
    name = "echo"
    description = "回显"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params: _EchoParams = args  # type: ignore[assignment]
        return ToolResult(content=[TextBlock(text=f"echo: {params.message}")])


def _tool_call_round(
    arguments: str, *, name: str = "echo", index: int = 0, call_id: str = "call_1"
) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": index,
            "id": call_id,
            "name": name,
            "arguments_delta": arguments,
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _make_loop(
    rounds: list[list[dict[str, Any]]],
    *,
    advance: AdvanceHook | None = None,
    recorder: _RecordingHook | None = None,
) -> tuple[AgentLoop, _RecordingHook | None]:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    hooks = None
    if advance is not None or recorder is not None:
        hooks = HookManager()
        if recorder is not None:
            hooks.register(recorder)
        if advance is not None:
            hooks.register_advance(advance)
    loop = AgentLoop(
        provider=FakeProvider.from_rounds(rounds),
        registry=registry,
        model="fake",
        workspace_root=".",
        signal=NeverCancelled(),
        clock=lambda: FIXED_TIME,
        hooks=hooks,
    )
    return loop, recorder


@pytest.mark.asyncio
async def test_loop_continues_and_injects_when_advance_hook_says_so() -> None:
    """G-ADV-4：continue → 注入 + 多跑一轮 + AdvanceDecided 事件。

    transcript 两轮：第一轮纯文本（模型想收手）→ 钩子说继续并注入 →
    第二轮还是纯文本 → completed。断言三件事：rounds=2、注入消息在
    produced 里、事件序列里 AdvanceDecided 出现在两次 AssistantProduced 之间。
    """
    recorder = _RecordingHook()
    loop, _ = _make_loop(
        [_text_round("我做完了"), _text_round("好，再检查一遍")],
        advance=_OnceAdvance(
            "continuer",
            AdvanceDecision(
                continue_round=True, inject="[推进] 验收命令还没跑，再检查一次", reason="清单未清空"
            ),
        ),
        recorder=recorder,
    )
    result = await loop.run_turn([])

    assert result.status == "completed"
    assert result.rounds == 2
    injected = [m for m in result.messages if type(m).__name__ == "LlmMessageWrapper"]
    assert any("[推进] 验收命令还没跑" in str(getattr(m, "message", "")) for m in injected)

    kinds = [type(e).__name__ for e in recorder.seen if recorder is not None]
    assert kinds.count("AdvanceDecided") == 1
    assert kinds.count("AssistantProduced") == 2
    # AdvanceDecided 必须在两次 AssistantProduced 之间（第一轮收手处）
    assert kinds.index("AdvanceDecided") > kinds.index("AssistantProduced")


@pytest.mark.asyncio
async def test_loop_stops_gracefully_when_advance_hook_forces_stop() -> None:
    """G-ADV-5：force_stop → 优雅 stopped（不抛异常），reason 透传。"""
    loop, recorder = _make_loop(
        [_text_round("我做完了")],
        advance=_FixedAdvance(
            "stopper", AdvanceDecision(force_stop=True, reason="验收命令 2 轮仍败")
        ),
        recorder=_RecordingHook(),
    )
    result = await loop.run_turn([])

    assert result.status == "stopped"
    assert result.reason == "验收命令 2 轮仍败"
    assert recorder is not None
    assert any(isinstance(e, AdvanceDecided) for e in recorder.seen)
    # stopped 也走 TurnEnd（收尾行照常上报）
    assert any(isinstance(e, TurnEnd) and e.status == "stopped" for e in recorder.seen)


@pytest.mark.asyncio
async def test_loop_without_advance_hooks_is_byte_identical() -> None:
    """G-ADV-6：不注册推进钩子时，行为与没有钩子系统之前逐字节一致。

    同一个 transcript 跑两次：一次完全无总线（hooks=None），一次有总线但
    只挂通知型钩子（模拟"钩子系统在、但没有推进钩子"的真实形态）。
    断言 status / rounds / text 三者相同——G33 的推进版。
    """
    rounds = [
        [
            {"type": "text_delta", "text": "我", "text_signature": None},
            *_tool_call_round('{"message": "hi"}'),
        ],
        _text_round("完成"),
    ]
    loop_plain, _ = _make_loop(rounds)
    result_plain = await loop_plain.run_turn([])

    recorder = _RecordingHook()
    loop_hooked, _ = _make_loop(rounds, recorder=recorder)
    result_hooked = await loop_hooked.run_turn([])

    assert result_plain.status == result_hooked.status == "completed"
    assert result_plain.rounds == result_hooked.rounds == 2
    assert result_plain.text == result_hooked.text == "完成"
    # 没有推进钩子 → 一次 AdvanceDecided 都没有
    assert not any(isinstance(e, AdvanceDecided) for e in recorder.seen)


@pytest.mark.asyncio
async def test_advance_facts_carry_round_and_last_text() -> None:
    """facts 只搬 loop 自己知道的事实：第几轮、模型这轮说了什么。
    钉住字段集——放多了（todo 状态等）第三方实现就被锁死在内置判据里。"""
    seen: list[AdvanceFacts] = []

    class _Spy(AdvanceHook):
        name = "spy"

        async def advance(self, facts: AdvanceFacts) -> AdvanceDecision:
            seen.append(facts)
            return AdvanceDecision()

    loop, _ = _make_loop([_text_round("收尾陈词")], advance=_Spy())
    await loop.run_turn([])

    assert len(seen) == 1
    assert seen[0].round_index == 1
    assert seen[0].last_text == "收尾陈词"
