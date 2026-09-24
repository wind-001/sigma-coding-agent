"""task 工具（sub_agent 派发 + 信箱）与 loop 钩子的门禁测试。

三个注入靶子：
    G84（子 registry 无 task）→ ``tests/test_sub_agent.py``；
    G85（截断可见）           → ``test_result_truncated_with_visible_marker``；
    G86（收尾兜底不丢结果）   → ``test_loop_waits_for_pending_subtasks_before_finishing``。

loop 级用例的轮次形状：loop 在"模型不再要工具"的轮结束；收尾兜底会**多跑一轮**
（把信箱回报给模型看），所以轮次表比直觉多一条纯文本轮。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field

from sigma_agent.agent_messages import LlmMessageWrapper
from sigma_agent.base import BaseTool
from sigma_agent.loop import AgentLoop
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import ToolContext, ToolResult, TurnResult
from sigma_ai.base import NeverCancelled
from sigma_ai.fake import FakeProvider
from sigma_ai.messages import TextBlock, Usage

from sigma_tools.task import MAX_RESULT_CHARS, TaskParams, TaskTool


# ---------------------------------------------------------------------------
# 假工厂与公共基建
# ---------------------------------------------------------------------------


class _FakeFactory:
    """可控的子 agent 工厂：延迟、结果、异常都能摆布，并记录并发峰值。"""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        text: str = "子任务结论",
        rounds: int = 3,
        status: str = "completed",
        error: Exception | None = None,
        usage: Usage | None = None,
    ) -> None:
        self.delay = delay
        self.text = text
        self.rounds = rounds
        self.status = status
        self.error = error
        self.usage = usage
        self.calls: list[str] = []
        self.max_rounds_seen: list[int] = []
        self.active = 0
        self.peak = 0

    async def __call__(
        self, description: str, ctx: ToolContext, sub_session_id: str,
        max_rounds: int = 0,
    ) -> TurnResult:
        self.calls.append(sub_session_id)
        # 记录派发方给的轮数预算——三档预算的断言就看它。
        self.max_rounds_seen.append(max_rounds)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return TurnResult(
                status=self.status,
                messages=[],
                text=self.text,
                rounds=self.rounds,
                usage=self.usage,
            )
        finally:
            self.active -= 1


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="sess-1", workspace_root=tmp_path, signal=NeverCancelled()
    )


def _tool(factory: _FakeFactory, *, max_concurrent: int = 3) -> TaskTool:
    return TaskTool(factory, max_concurrent=max_concurrent)


async def _dispatch(
    tool: TaskTool,
    tmp_path: Path,
    description: str = "调查 X",
    *,
    difficulty: str = "medium",
) -> ToolResult:
    return await tool.run(
        TaskParams(
            action="dispatch", description=description, difficulty=difficulty
        ),
        _ctx(tmp_path),
    )


def _user_texts(messages: list[Any]) -> list[str]:
    """抽取信箱回报类 user 消息文本（LlmMessageWrapper(UserMessage)）。"""
    found: list[str] = []
    for m in messages:
        inner = getattr(m, "message", None)
        content = getattr(inner, "content", None)
        if isinstance(content, str):
            found.append(content)
    return found


# ---------------------------------------------------------------------------
# dispatch 与信箱
# ---------------------------------------------------------------------------


async def test_dispatch_returns_immediately(tmp_path: Path) -> None:
    """dispatch 必须立即返回（后台执行）——工厂没跑完，返回里也不能有结果文本。"""
    factory = _FakeFactory(delay=5.0)
    tool = _tool(factory)
    result = await _dispatch(tool, tmp_path)
    assert result.is_error is False
    text = result.content[0].text
    assert "#sa1" in text
    assert "子任务结论" not in text, "结果还没跑出来，不能出现在返回里"
    assert tool.pending_count() == 1
    # 清理：不让后台任务悬到测试之外
    await tool.wait_and_drain()


async def test_completed_result_arrives_via_drain(tmp_path: Path) -> None:
    factory = _FakeFactory(text="结论：42", rounds=7)
    tool = _tool(factory)
    await _dispatch(tool, tmp_path)
    drained = await tool.wait_and_drain()
    assert len(drained) == 1
    body = _user_texts(drained)[0]
    assert "[子任务回报] #sa1" in body
    assert "结论：42" in body
    assert "7 轮" in body


async def test_result_truncated_with_visible_marker(tmp_path: Path) -> None:
    """G85 的靶子：超长结果必须截断，且截断这件事本身要可见。"""
    factory = _FakeFactory(text="长" * (MAX_RESULT_CHARS + 500))
    tool = _tool(factory)
    await _dispatch(tool, tmp_path)
    body = _user_texts(await tool.wait_and_drain())[0]
    assert "已截断" in body
    assert len(body) < MAX_RESULT_CHARS + 200, "截断上限必须真的生效"


async def test_stopped_result_marked_untrusted(tmp_path: Path) -> None:
    """子会话轮数耗尽（stopped）：结果可信度必须与结果一起交给模型。"""
    factory = _FakeFactory(status="stopped", text="只查到一半")
    tool = _tool(factory)
    await _dispatch(tool, tmp_path)
    body = _user_texts(await tool.wait_and_drain())[0]
    assert "未正常收尾" in body
    assert "只查到一半" in body


async def test_factory_exception_reported_not_silent(tmp_path: Path) -> None:
    """后台任务抛异常：必须变成信箱里的可见失败回报，不能静默消失。"""
    factory = _FakeFactory(error=ValueError("boom"))
    tool = _tool(factory)
    await _dispatch(tool, tmp_path)
    body = _user_texts(await tool.wait_and_drain())[0]
    assert "执行失败" in body
    assert "ValueError: boom" in body


async def test_drain_marks_delivered_idempotent(tmp_path: Path) -> None:
    """同一结果只回报一次（delivered 标记），重复 drain 为空。"""
    factory = _FakeFactory()
    tool = _tool(factory)
    await _dispatch(tool, tmp_path)
    first = await tool.wait_and_drain()
    assert len(first) == 1
    assert await tool.wait_and_drain() == []
    assert tool.drain_completed() == []


async def test_report_includes_token_usage(tmp_path: Path) -> None:
    """D-A1（P4-批次2）：子 agent 的 token 用量必须随回报回传——
    否则 A/B 评测的成本指标只统计到主 agent（编排者），两臂真实花费不可见。"""
    factory = _FakeFactory(
        text="结论",
        usage=Usage(prompt_tokens=1234, completion_tokens=56, cached_tokens=100),
    )
    tool = _tool(factory)
    await _dispatch(tool, tmp_path)
    body = _user_texts(await tool.wait_and_drain())[0]
    assert "1234+56" in body
    assert "cached 100" in body


async def test_report_without_usage_byte_identical(tmp_path: Path) -> None:
    """usage=None（回放/假工厂默认路径）时回报文本与加 D-A1 之前逐字节一致。"""
    factory = _FakeFactory(text="结论", rounds=7)
    tool = _tool(factory)
    await _dispatch(tool, tmp_path)
    body = _user_texts(await tool.wait_and_drain())[0]
    assert "已完成（7 轮）：" in body
    assert "token" not in body


async def test_status_reports_states(tmp_path: Path) -> None:
    factory = _FakeFactory(delay=0.2)
    tool = _tool(factory)
    await _dispatch(tool, tmp_path, "任务一")
    await asyncio.sleep(0.01)  # 让事件循环调度后台协程：queued → running
    result = await tool.run(TaskParams(action="status"), _ctx(tmp_path))
    text = result.content[0].text
    assert "#sa1 [running]" in text
    # 跑完后再查：completed 且标已回报
    await tool.wait_and_drain()
    result2 = await tool.run(TaskParams(action="status"), _ctx(tmp_path))
    assert "#sa1 [completed]（已回报）" in result2.content[0].text


async def test_concurrency_capped_at_three(tmp_path: Path) -> None:
    """星辰拍板：同时最多 3 个子 agent（token 成本闸），第 4 个起排队。"""
    factory = _FakeFactory(delay=0.05)
    tool = _tool(factory, max_concurrent=3)
    for i in range(5):
        await _dispatch(tool, tmp_path, f"任务 {i}")
    await tool.wait_and_drain()
    assert factory.peak == 3, f"并发峰值应为 3，实际 {factory.peak}"
    assert len(factory.calls) == 5, "排队的任务最终也要全部跑完"


async def test_dispatch_requires_description(tmp_path: Path) -> None:
    tool = _tool(_FakeFactory())
    result = await _dispatch(tool, tmp_path, description="   ")
    assert result.is_error is True
    assert tool.pending_count() == 0


async def test_derived_session_ids(tmp_path: Path) -> None:
    """派生 id = {主会话id}-sa{n}，同一实例内递增——日志与评测靠它对回派发来源。"""
    factory = _FakeFactory()
    tool = _tool(factory)
    await _dispatch(tool, tmp_path)
    await tool.wait_and_drain()
    await _dispatch(tool, tmp_path)
    await tool.wait_and_drain()
    assert factory.calls == ["sess-1-sa1", "sess-1-sa2"]


# ---------------------------------------------------------------------------
# loop 级：轮顶部 drain、收尾兜底、写批次锁
# ---------------------------------------------------------------------------


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _call_round(call_id: str, name: str, **arguments: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": call_id,
            "name": name,
            "arguments_delta": json.dumps(arguments, ensure_ascii=False),
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _loop(
    provider: FakeProvider,
    tmp_path: Path,
    *,
    tools: list[BaseTool] | None = None,
    lock: asyncio.Lock | None = None,
    mailbox_drain: Any = None,
    mailbox_wait: Any = None,
) -> AgentLoop:
    registry = ToolRegistry()
    for tool in tools or []:
        registry.register(tool)
    return AgentLoop(
        provider=provider,
        registry=registry,
        model="fake",
        workspace_root=tmp_path,
        signal=NeverCancelled(),
        tool_lock=lock,
        mailbox_drain=mailbox_drain,
        mailbox_wait=mailbox_wait,
    )


class _EchoParams(BaseModel):
    message: str = Field(description="要回显的内容")


class _EchoTool(BaseTool):
    """撑轮次用的回显工具（与 test_todo_steering._EchoTool 同款）。"""

    name = "echo"
    description = "回显 message"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(_EchoParams, args)
        return ToolResult(content=[TextBlock(text=f"echo: {params.message}")])


def _echo_round(call_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": call_id,
            "name": "echo",
            "arguments_delta": '{"message": "hi"}',
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


async def test_drain_injected_at_turn_start(tmp_path: Path) -> None:
    """信箱有未回报结果时，下一轮开始前必须注入（模型本轮就能看到）。"""
    from sigma_ai.messages import UserMessage

    pending = [
        LlmMessageWrapper(
            timestamp="t",
            message=UserMessage(content="[子任务回报] #sa1 已完成：结论 A", timestamp="t"),
        )
    ]
    box = {"items": list(pending)}

    def drain() -> list[Any]:
        items = box["items"]
        box["items"] = []
        return items

    async def wait() -> list[Any]:
        return box["items"]

    # echo 轮撑住循环（纯文本轮会立刻收尾），drain 在第 1 轮顶部注入
    loop = _loop(
        FakeProvider.from_rounds([_echo_round("c1"), _text_round("完成")]),
        tmp_path,
        tools=[_EchoTool()],
        mailbox_drain=drain,
        mailbox_wait=wait,
    )
    result = await loop.run_turn([])
    assert result.rounds == 2
    bodies = [
        t
        for t in _user_texts(result.messages)
        if "子任务回报" in t
    ]
    assert len(bodies) == 1, "回报必须在 produced 里，且只注入一次"


async def test_loop_waits_for_pending_subtasks_before_finishing(tmp_path: Path) -> None:
    """G86 的靶子：模型 dispatch 后直接收尾，loop 必须等子任务完成、注入回报、
    再多跑一轮——结果绝不能丢在信箱里。

    轮次：r1 dispatch；r2 文本收尾（触发兜底等待+注入）；r3 模型看到回报后真正收尾。
    工厂 delay 保证 dispatch 返回时子任务仍在跑（等待是真的等过）。
    """
    factory = _FakeFactory(delay=0.05, text="子任务结论 B", rounds=2)
    task_tool = _tool(factory)
    loop = _loop(
        FakeProvider.from_rounds(
            [
                _call_round("c1", "task", action="dispatch", description="查 B"),
                _text_round("已派发"),
                _text_round("收到结论 B，全部完成"),
            ]
        ),
        tmp_path,
        tools=[task_tool],
        mailbox_drain=task_tool.drain_completed,
        mailbox_wait=task_tool.wait_and_drain,
    )
    result = await loop.run_turn([])
    assert result.rounds == 3, "收尾兜底应多跑一轮"
    bodies = [t for t in _user_texts(result.messages) if "子任务回报" in t]
    assert len(bodies) == 1 and "子任务结论 B" in bodies[0]
    # 回报不在序列末尾：后面还有模型对回报的响应
    index = next(
        i
        for i, m in enumerate(result.messages)
        if "子任务回报" in (_user_texts([m])[0] if _user_texts([m]) else "")
    )
    assert index < len(result.messages) - 1


class _ProbeParams(BaseModel):
    pass


class _LockProbeTool(BaseTool):
    """记录执行瞬间 tool_lock 是否被持有（写批次互斥的直接证据）。"""

    name = "lockprobe"
    description = "探测锁状态"
    read_only = False

    def __init__(self, lock: asyncio.Lock | None) -> None:
        self._lock = lock
        self.seen_locked: list[bool | None] = []

    @property
    def params(self) -> type[BaseModel]:
        return _ProbeParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        if self._lock is None:
            self.seen_locked.append(None)
        else:
            self.seen_locked.append(self._lock.locked())
        return ToolResult(content=[TextBlock(text="probed")])


async def test_write_batch_runs_under_tool_lock(tmp_path: Path) -> None:
    lock = asyncio.Lock()
    probe = _LockProbeTool(lock)
    loop = _loop(
        FakeProvider.from_rounds([_call_round("c1", "lockprobe"), _text_round("ok")]),
        tmp_path,
        tools=[probe],
        lock=lock,
    )
    await loop.run_turn([])
    assert probe.seen_locked == [True], "写批次必须在 tool_lock 内执行"


async def test_write_batch_unaffected_without_lock(tmp_path: Path) -> None:
    """tool_lock=None：行为与加它之前逐字节一致（默认路径承诺）。"""
    probe = _LockProbeTool(None)
    loop = _loop(
        FakeProvider.from_rounds([_call_round("c1", "lockprobe"), _text_round("ok")]),
        tmp_path,
        tools=[probe],
        lock=None,
    )
    await loop.run_turn([])
    assert probe.seen_locked == [None]


# ---------------------------------------------------------------------------
# 难度档位 → 轮数预算（G89 的靶子）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("difficulty", "expected"), [("low", 10), ("medium", 20), ("high", 30)]
)
async def test_difficulty_selects_round_budget(
    tmp_path: Path, difficulty: str, expected: int
) -> None:
    """**G89 的靶子**：难度档位真的决定子会话的轮数预算。

    注入（``for_level`` 恒返回 high）后：low 档也会拿到 30 → 本用例红。
    断言落在"工厂收到的值"上——那是唯一能证明预算真的被传下去的地方。
    """
    factory = _FakeFactory()
    tool = _tool(factory)
    await _dispatch(
        tool, tmp_path, "查一下", **{"difficulty": difficulty}
    )
    await tool.wait_and_drain()
    assert factory.max_rounds_seen == [expected]


async def test_default_difficulty_is_medium(tmp_path: Path) -> None:
    """不指定难度 = medium（20 轮）——与主任务默认轮数一致，行为不漂移。"""
    factory = _FakeFactory()
    tool = _tool(factory)
    await _dispatch(tool, tmp_path, "查一下")
    await tool.wait_and_drain()
    assert factory.max_rounds_seen == [20]


async def test_dispatch_receipt_shows_budget(tmp_path: Path) -> None:
    """预算必须对模型可见：它才知道"这个子任务被给了多少轮"，
    也才可能在 low 档跑不完时用 high 重派。"""
    tool = _tool(_FakeFactory())
    result = await _dispatch(tool, tmp_path, "复杂修复", difficulty="high")
    text = result.content[0].text  # type: ignore[union-attr]
    assert "轮数预算 30" in text
    assert "难度 high" in text


async def test_unknown_difficulty_rejected_by_schema(tmp_path: Path) -> None:
    """档位名由 Literal 约束：拼错的档位在参数校验层就被拒，不进信箱。"""
    tool = _tool(_FakeFactory())
    with pytest.raises(Exception):
        await tool.run(
            TaskParams.model_construct(
                action="dispatch", description="x", difficulty="ultra"
            ),
            _ctx(tmp_path),
        )
