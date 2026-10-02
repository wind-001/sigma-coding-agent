"""LoopGuard 单测（LoopControl 批次 1）。

口径与评审意见一一对应（必修 1/2/3/4/5）：
- 同签名**失败**调用第 2 次 continue、第 3 次必停（必修 1 修正 + 必修 2 反向断言）；
- 连续 4 错 continue、连续 5 错必停（必修 2 反向断言）；
- 成功调用的原地踏步不进硬闸（必修 3 口径：硬闸仅适用失败调用）；
- 停滞 → nudge → 恢复 → 再停滞 → 重新获得 nudge 机会（必修 4）；
- 审批等待期间墙钟挂起（必修 5）。

全部确定性可复现：时间用受控假时钟，无 sleep、无随机。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field

from sigma.agent.messages import LlmMessageWrapper, UserMessage
from sigma.agent.types import ToolContext, ToolResult
from sigma.providers.base import CancelToken, NeverCancelled
from sigma.tools.base import BaseTool
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock, UserMessage
from sigma.runtime.event_loop import AgentLoop
from sigma.runtime.loop_guard import (
    CallFacts,
    GuardConfig,
    LoopGuard,
    normalize_args,
    result_fingerprint,
)
from sigma.tools.registry import ToolRegistry

FIXED_TIME = "2026-10-02 12:00:00.000"


# ---------------------------------------------------------------------------
# 纯护栏单测：不经过 loop，直接驱动 LoopGuard
# ---------------------------------------------------------------------------


def _failed_call(sig: str, result: str = "stderr: boom") -> CallFacts:
    return CallFacts(
        name="failing", args_signature=sig, result_signature=result_fingerprint(result), ok=False
    )


def _ok_call(sig: str, result: str) -> CallFacts:
    return CallFacts(
        name="echo", args_signature=sig, result_signature=result_fingerprint(result), ok=True
    )


def test_same_signature_second_failure_continues() -> None:
    """必修 2 反向断言：同签名失败第 2 次必须 continue（off-by-one 的红灯）。"""
    guard = LoopGuard(GuardConfig())
    guard.end_round(1)
    guard.observe_call(_failed_call('{"q":"a"}'))
    verdict = guard.end_round(2, prompt_tokens=10)
    assert verdict.action == "continue"


def test_same_signature_failure_stops_on_third_call() -> None:
    """同签名失败调用 3 次必停（必修 3 口径：硬闸仅适用失败调用）。"""
    guard = LoopGuard(GuardConfig())
    for round_index in (1, 2, 3):
        guard.observe_call(_failed_call('{"q":"a"}'))
        verdict = guard.end_round(round_index)
    assert verdict.action == "stop"
    assert verdict.layer == "hard"
    assert "同参数" in verdict.reason
    assert "3 次" in verdict.reason


def test_successful_identical_calls_never_trip_hard_gate() -> None:
    """必修 3：同签名硬闸只数失败调用——成功调用哪怕参数完全相同也不停。

    结果各不相同（真实进展），S1 也不应触发。
    """
    guard = LoopGuard(GuardConfig())
    verdict: Any = None
    for round_index in range(1, 11):
        guard.observe_call(_ok_call('{"q":"poll"}', result=f"result #{round_index}"))
        verdict = guard.end_round(round_index)
    assert verdict.action == "continue"


def test_four_consecutive_errors_continue() -> None:
    """必修 2 反向断言：连续 4 错必须 continue（不能第 4 次就停）。"""
    guard = LoopGuard(GuardConfig())
    verdict: Any = None
    for round_index in range(1, 5):
        guard.observe_call(_failed_call(f'{{"q":"v{round_index}"}}', result=f"err {round_index}"))
        verdict = guard.end_round(round_index)
    assert verdict.action == "continue"


def test_five_consecutive_errors_stop() -> None:
    """连续 5 错必停——失败签名各不相同，排除同签名闸先触发。"""
    guard = LoopGuard(GuardConfig())
    for round_index in range(1, 6):
        guard.observe_call(_failed_call(f'{{"q":"v{round_index}"}}', result=f"err {round_index}"))
        verdict = guard.end_round(round_index)
    assert verdict.action == "stop"
    assert "连续 5 次工具失败" in verdict.reason


def test_identical_results_window_nudge_nudge_stop() -> None:
    """S1：窗口内结果指纹全同 → 第 1 次 nudge、第 2 次 nudge、第 3 次 stop。

    参数各不相同（排除 S2/同签名闸），只有"世界没变"这一条信号。
    """
    guard = LoopGuard(GuardConfig())
    actions: list[str] = []
    for round_index in range(1, 6):
        guard.observe_call(_ok_call(f'{{"q":"v{round_index}"}}', result="固定输出"))
        verdict = guard.end_round(round_index)
        actions.append(verdict.action)
    assert actions == ["continue", "continue", "nudge", "nudge", "stop"]
    assert verdict.layer == "soft"


def test_cyclic_action_pattern_detected() -> None:
    """S2：A,B,A,B 振荡且结果按同周期重复（真·原地打转）→ 第 4 轮 nudge。

    反向口径藏在同一条里：参数循环但**结果在变**（读→改→再读的健康节奏）
    不算停滞——test_read_edit_cycle_is_not_a_stall 单独盖。
    """
    guard = LoopGuard(GuardConfig())
    actions: list[str] = []
    for round_index, sig in enumerate(["A", "B", "A", "B"], start=1):
        result = "输出 r1" if round_index % 2 == 1 else "输出 r2"
        guard.observe_call(_ok_call(sig, result=result))
        verdict = guard.end_round(round_index)
        actions.append(verdict.action)
    assert actions == ["continue", "continue", "continue", "nudge"]


def test_read_edit_cycle_is_not_a_stall() -> None:
    """健康节奏反例：读→改→再读（参数 A,B,A,B 周期）但结果每次都变
    （文件在被推进）——绝不能误判停滞。这是 S2 要求结果同周期重复的原因。"""
    guard = LoopGuard(GuardConfig())
    for round_index in range(1, 13):
        sig = "A" if round_index % 2 == 1 else "B"
        guard.observe_call(_ok_call(sig, result=f"文件内容 v{round_index}"))
        verdict = guard.end_round(round_index)
        assert verdict.action == "continue", f"第 {round_index} 轮误判: {verdict.reason}"


def test_healthy_progress_all_continue() -> None:
    """反向护栏：参数与结果都持续变化的健康任务，30 轮全程 continue。"""
    guard = LoopGuard(GuardConfig())
    for round_index in range(1, 31):
        guard.observe_call(_ok_call(f'{{"step":{round_index}}}', result=f"进展 #{round_index}"))
        verdict = guard.end_round(round_index)
        assert verdict.action == "continue", f"第 {round_index} 轮误触发: {verdict.reason}"


def test_absolute_round_fuse_at_200() -> None:
    """绝对轮数保险丝：None 语义下第 200 轮必停、第 199 轮 continue。"""
    guard = LoopGuard(GuardConfig())  # 默认 max_rounds=200
    verdict: Any = None
    for round_index in range(1, 200):
        verdict = guard.end_round(round_index)
        assert verdict.action == "continue"
    verdict = guard.end_round(200)
    assert verdict.action == "stop"
    assert "200" in verdict.reason


def test_token_budget_stops() -> None:
    """token 预算：累计达到阈值即停（纯文本轮同样生效）。"""
    guard = LoopGuard(GuardConfig(token_budget=100))
    guard.end_round(1, prompt_tokens=60)
    verdict = guard.end_round(2, prompt_tokens=60)  # 累计 120 ≥ 100
    assert verdict.action == "stop"
    assert "token" in verdict.reason


def test_wall_clock_excludes_approval_pause() -> None:
    """必修 5：审批等待期间墙钟挂起——挂了 1995s 也不算失控。"""
    tick = {"t": 0.0}

    def fake_now() -> float:
        return tick["t"]

    guard = LoopGuard(GuardConfig(wall_clock_s=30.0), now=fake_now)
    tick["t"] = 5.0
    guard.pause()  # 审批框弹出
    tick["t"] = 2000.0  # 人想了 1995 秒
    guard.resume()
    tick["t"] = 2010.0  # 真实推进 10s（扣掉挂起后）
    verdict = guard.end_round(1)
    assert verdict.action == "continue"  # 不挂起的话这里早已超时

    tick["t"] = 2040.0  # 再真实跑 30s
    verdict = guard.end_round(2)
    assert verdict.action == "stop"
    assert "不含审批等待" in verdict.reason


def test_nudge_budget_resets_after_recovery() -> None:
    """必修 4：停滞 → nudge → 恢复（连续两轮干净）→ 再停滞 → 重新获得 nudge。

    没有重置语义的话，第二次停滞会无预警直接 stop。
    窗口用 3：滑动窗口里旧停滞轮滑出之前 S1 不可能再触发，
    断言粒度才够细（窗口 6 时第 2 段停滞要到第 11 轮才可见）。
    """
    guard = LoopGuard(GuardConfig(stall_window=3, stall_min_actions=3))
    # 第 1 段停滞：3 轮同结果 → 第 3 轮首次 nudge（配额 2 用掉 1）
    for round_index in range(1, 4):
        guard.observe_call(_ok_call(f'{{"q":"a{round_index}"}}', result="卡住"))
        verdict = guard.end_round(round_index)
    assert verdict.action == "nudge"
    # 恢复：2 轮干净（新结果）→ 连续两个观察未触发 → 配额清零
    for round_index in range(4, 6):
        guard.observe_call(_ok_call(f'{{"q":"b{round_index}"}}', result=f"进展 #{round_index}"))
        verdict = guard.end_round(round_index)
        assert verdict.action == "continue"
    # 第 2 段停滞：3 轮同结果 → 应重新获得 nudge（而不是直接 stop）
    for round_index in range(6, 9):
        guard.observe_call(_ok_call(f'{{"q":"c{round_index}"}}', result="又卡住"))
        verdict = guard.end_round(round_index)
    assert verdict.action == "nudge"


def test_pure_text_rounds_do_not_trigger_soft_detection() -> None:
    """纯文本轮不参与停滞软检测（窗口只收工具轮），但硬闸照常生效。"""
    guard = LoopGuard(GuardConfig())
    for round_index in range(1, 11):
        verdict = guard.end_round(round_index, prompt_tokens=10)
        assert verdict.action == "continue"
        assert verdict.layer == "none"


def test_soft_detection_can_be_disabled() -> None:
    """soft_enabled=False：同结果停滞不触发 nudge/stop，硬闸照常。"""
    # 对照组：软检测开着 → 同结果停滞最迟第 5 轮被截断
    guard_on = LoopGuard(GuardConfig())
    on_actions: list[str] = []
    for round_index in range(1, 6):
        guard_on.observe_call(_ok_call(f'{{"q":"v{round_index}"}}', result="固定输出"))
        on_actions.append(guard_on.end_round(round_index).action)
    assert "stop" in on_actions or "nudge" in on_actions
    # 实验组：软检测关掉 → 同样的输入 5 轮全程 continue（硬闸未触发：
    # 调用全部成功，同签名失败闸/连续错误闸均不适用）
    guard_off = LoopGuard(GuardConfig(soft_enabled=False))
    for round_index in range(1, 6):
        guard_off.observe_call(_ok_call(f'{{"q":"v{round_index}"}}', result="固定输出"))
        verdict = guard_off.end_round(round_index)
        assert verdict.action == "continue"


def test_disabled_config_is_fully_inert() -> None:
    """GuardConfig.disabled()（子 agent 口径）：连硬闸也全关——
    20 次同签名失败调用全程 continue，行为与没有护栏时逐字节一致。"""
    guard = LoopGuard(GuardConfig.disabled())
    for round_index in range(1, 21):
        guard.observe_call(_failed_call('{"q":"same"}'))
        verdict = guard.end_round(round_index)
        assert verdict.action == "continue"
    assert all(v.layer == "none" for _, v in guard.verdicts)


# ---------------------------------------------------------------------------
# 集成：AgentLoop + 护栏（FakeProvider 驱动，全确定性）
# ---------------------------------------------------------------------------


class _ConstParams(BaseModel):
    message: str = Field(description="参数")


class _ConstTool(BaseTool):
    """成功但输出恒定——S1 停滞的集成触发器。"""

    name = "const"
    description = "永远返回同一输出"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _ConstParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text="固定输出")])


class _FailingTool(BaseTool):
    """按约定返回 is_error 的失败工具。"""

    name = "failing"
    description = "总是失败"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _ConstParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text="stderr: boom")], is_error=True)


def _tool_round(arguments: str, *, name: str, call_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
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


def _history() -> list[LlmMessageWrapper]:
    return [
        LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="开始任务", timestamp=FIXED_TIME),
        )
    ]


def _make_loop(rounds: list[list[dict[str, Any]]], tools: list[Any]) -> tuple[AgentLoop, FakeProvider]:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    provider = FakeProvider.from_rounds(rounds)
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        model="fake",
        workspace_root=".",
        signal=NeverCancelled(),
        clock=lambda: FIXED_TIME,
    )
    return loop, provider


@pytest.mark.asyncio
async def test_loop_stops_on_repeated_identical_failures() -> None:
    """集成：同一失败调用 3 次 → loop 优雅停止，reason 带同签名口径。"""
    loop, provider = _make_loop(
        [
            _tool_round('{"message": "x"}', name="failing", call_id="c1"),
            _tool_round('{"message": "x"}', name="failing", call_id="c2"),
            _tool_round('{"message": "x"}', name="failing", call_id="c3"),
        ],
        tools=[_FailingTool()],
    )
    result = await loop.run_turn(_history())
    assert result.status == "stopped"
    assert result.rounds == 3
    assert "同参数" in (result.reason or "")
    assert provider.remaining_rounds == 0
    assert loop.last_guard is not None
    assert loop.last_guard.verdicts[-1][1].action == "stop"


@pytest.mark.asyncio
async def test_healthy_task_completes_and_guard_all_continue() -> None:
    """集成：健康任务（1 工具轮 + 文本收尾）completed，护栏全程 continue。"""
    loop, provider = _make_loop(
        [_tool_round('{"message": "hi"}', name="const", call_id="c1"), _text_round("完成")],
        tools=[_ConstTool()],
    )
    result = await loop.run_turn(_history())
    assert result.status == "completed"
    assert provider.remaining_rounds == 0
    assert loop.last_guard is not None
    assert all(v.action == "continue" for _, v in loop.last_guard.verdicts)


@pytest.mark.asyncio
async def test_nudge_message_reaches_model_as_injected_user_message() -> None:
    """集成：S1 停滞 → nudge 以尾部 user 消息进 produced（steering 同模式），
    模型第 4 轮看到后正常收尾 → completed。"""
    loop, provider = _make_loop(
        [
            _tool_round('{"message": "a"}', name="const", call_id="c1"),
            _tool_round('{"message": "b"}', name="const", call_id="c2"),
            _tool_round('{"message": "c"}', name="const", call_id="c3"),
            _text_round("已汇总卡点,收尾"),
        ],
        tools=[_ConstTool()],
    )
    result = await loop.run_turn(_history())
    assert result.status == "completed"
    nudges = [
        m
        for m in result.messages
        if isinstance(m, LlmMessageWrapper)
        and isinstance(m.message, UserMessage)
        and "[停滞提醒]" in str(m.message.content)
    ]
    assert len(nudges) == 1
