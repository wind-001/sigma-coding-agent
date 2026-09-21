"""loop 观测接口的测试（门槛 G32 / G33 / G36 的 loop 侧）。

为什么这些 helper 不从 ``test_agent_loop.py`` 里 import
    那个文件里已经有一套同形的 helper，但搬过去共享要改一个承载着
    226 条既有用例的文件——**为了新测试去动老文件不划算**。
    这里是最小副本，且**不共享状态**：测试之间互相依赖会让"改一个文件
    崩两个测试"成为常态。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from sigma_agent.loop import AgentLoop
from sigma_agent.observe import (
    LoopObserver,
    TextChunk,
    ToolEnd,
    ToolStart,
    TurnEnd,
)
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.base import NeverCancelled
from sigma_ai.fake import FakeProvider
from sigma_ai.messages import TextBlock
from sigma_agent.base import BaseTool
from pydantic import BaseModel, Field

FIXED_TIME = 1_700_000_000


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
        params = cast(_EchoParams, args)
        return ToolResult(content=[TextBlock(text=f"echo: {params.message}")])


class _FailingTool(BaseTool):
    name = "failing"
    description = "按约定返回 is_error"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            content=[TextBlock(text="stderr: 退出码 1")],
            details={"exit_code": 1},
            is_error=True,
        )


class RecordingObserver(LoopObserver):
    """把所有事件记下来。测试断言的是**序列**，不只是"收到过"。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


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
    tool: BaseTool | None = None,
    observer: LoopObserver | None = None,
) -> tuple[AgentLoop, RecordingObserver | None]:
    registry = ToolRegistry()
    registry.register(tool if tool is not None else _EchoTool())
    loop = AgentLoop(
        provider=FakeProvider.from_rounds(rounds),
        registry=registry,
        model="fake",
        workspace_root=".",
        signal=NeverCancelled(),
        clock=lambda: FIXED_TIME,
        observer=observer,
    )
    return loop, observer


@pytest.mark.asyncio
async def test_loop_emits_full_event_sequence_in_order() -> None:
    """门槛 G32：一次 run_turn 吐出完整事件序列，且**顺序正确**。

    顺序是这条用例的重点：``ToolStart`` 必须早于 ``ToolEnd``——
    反了的话终端会显示"先出结果后出调用"。
    """
    observer = RecordingObserver()
    loop, _ = _make_loop(
        [
            [
                {"type": "text_delta", "text": "我", "text_signature": None},
                {"type": "text_delta", "text": "看一下", "text_signature": None},
                *_tool_call_round('{"message": "hi"}', name="echo"),
            ],
            _text_round("完成"),
        ],
        observer=observer,
    )

    await loop.run_turn([])

    kinds = [type(e).__name__ for e in observer.events]
    assert kinds == [
        "TextChunk",
        "TextChunk",
        "ToolStart",
        "ToolEnd",
        "TextChunk",
        "TurnEnd",
    ], f"事件序列不符：{kinds}"

    # 文本**逐块**透传，不聚合——聚合了就没有"流式"了
    assert [e.text for e in observer.events if isinstance(e, TextChunk)] == [
        "我",
        "看一下",
        "完成",
    ]

    start = next(e for e in observer.events if isinstance(e, ToolStart))
    end = next(e for e in observer.events if isinstance(e, ToolEnd))
    assert start.name == "echo"
    assert end.ok is True
    assert "echo: hi" in end.preview

    turn_end = observer.events[-1]
    assert isinstance(turn_end, TurnEnd)
    assert turn_end.status == "completed"


@pytest.mark.asyncio
async def test_observer_is_optional_and_default_impl_is_silent() -> None:
    """门槛 G33：不传 observer 时行为与加观测之前完全一致。

    ``LoopObserver`` 的 ``on_event`` 有**默认空实现**——这条断言钉住它：
    若哪天被改成抽象方法，"只想看一类事件"的观察者就得写一堆空方法。
    """
    loop_without, _ = _make_loop(
        [
            [*_tool_call_round('{"message": "hi"}')],
            _text_round("完成"),
        ]
    )
    result = await loop_without.run_turn([])
    assert result.status == "completed"

    # 默认实现可直接调用且不抛——这是"默认空实现"的直接证据
    LoopObserver().on_event(TextChunk(text="x"))


@pytest.mark.asyncio
async def test_failing_tool_emits_not_ok_with_visible_text() -> None:
    """门槛 G36（loop 侧）：失败的工具必须 ``ok=False`` **且失败文本进 preview**。

    只标 ok=False 而不带文本，终端上就只剩一个 ✗——人看不到失败原因，
    而"看到失败原因"是纠错的前提（详规 3.6）。
    """
    observer = RecordingObserver()
    loop, _ = _make_loop(
        [
            [*_tool_call_round('{"message": "hi"}', name="failing")],
            _text_round("看到了失败"),
        ],
        tool=_FailingTool(),
        observer=observer,
    )

    await loop.run_turn([])

    end = next(e for e in observer.events if isinstance(e, ToolEnd))
    assert end.ok is False
    assert "退出码 1" in end.preview
