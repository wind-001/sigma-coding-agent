"""steering（连续 N 轮未碰 todo 就提醒）的门禁测试。

需求（星辰，2026-09-23）：10 轮 turn 没有重复调用 todo 获取最新进度，
就强制提醒——防多轮 turn 塞满 context 后任务跑偏。

**这里的测试是 G83 注入的靶子**：把 ``_todo_steer_if_due`` 的注入分支
改成 ``if False``，``test_reminder_injected_after_interval`` 必须变红。

轮次构造的形状说明：loop 在"模型不再要工具"的那一轮结束，所以
"连续 N 轮没碰 todo"要用 N 个**工具调用轮**（echo）撑起来，
最后一轮纯文本收尾。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.loop import AgentLoop
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.base import NeverCancelled
from sigma_ai.fake import FakeProvider
from sigma_ai.messages import TextBlock

from sigma_tools.todo import TodoTool


class _EchoParams(BaseModel):
    message: str = Field(description="要回显的内容")


class _EchoTool(BaseTool):
    """本地最小回显工具（与 test_agent_loop.EchoTool 同款，避免跨测试文件 import）。"""

    name = "echo"
    description = "回显 message"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(_EchoParams, args)
        return ToolResult(content=[TextBlock(text=f"echo: {params.message}")])


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text},
        {"type": "stop", "stop_reason": "stop"},
    ]


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


def _todo_round(call_id: str, **arguments: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": call_id,
            "name": "todo",
            "arguments_delta": json.dumps(arguments, ensure_ascii=False),
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _loop(
    rounds: list[list[dict[str, Any]]],
    tmp_path: Path,
    *,
    interval: int = 10,
    with_todo: bool = True,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    if with_todo:
        registry.register(TodoTool())
    return AgentLoop(
        provider=FakeProvider.from_rounds(rounds),
        registry=registry,
        model="fake",
        workspace_root=tmp_path,
        signal=NeverCancelled(),
        todo_steer_interval=interval,
    )


def _reminder_texts(messages: list[Any]) -> list[str]:
    """抽出提醒文本。提醒是 LlmMessageWrapper(UserMessage)，content 是 str。"""
    found: list[str] = []
    for m in messages:
        inner = getattr(m, "message", None)
        content = getattr(inner, "content", None)
        if isinstance(content, str) and "任务清单提醒" in content:
            found.append(content)
    return found


# ---------------------------------------------------------------------------
# 计数与注入
# ---------------------------------------------------------------------------


async def test_reminder_injected_after_interval(tmp_path: Path) -> None:
    """interval=3：连续 3 轮没碰 todo，第 4 轮开始前必须出现提醒。G83 的靶子。"""
    loop = _loop(
        [_echo_round("c1"), _echo_round("c2"), _echo_round("c3"), _text_round("done")],
        tmp_path,
        interval=3,
    )
    result = await loop.run_turn([])
    assert result.rounds == 4
    reminders = _reminder_texts(result.messages)
    assert len(reminders) == 1, "第 4 轮前应注入恰好一条提醒"
    assert loop.todo_stall == 1, (
        "注入清零后，第 4 轮（文本收尾轮）没碰 todo 又计 1——语义正确"
    )


async def test_touching_todo_resets_stall(tmp_path: Path) -> None:
    """任何 todo 调用（哪怕结果不是成功）都清零——模型在看计划就是有意识。"""
    loop = _loop(
        [
            _echo_round("c1"),
            _echo_round("c2"),
            _todo_round("c3", action="list"),  # 碰了 → 清零
            _echo_round("c4"),
            _text_round("done"),
        ],
        tmp_path,
        interval=3,
    )
    result = await loop.run_turn([])
    assert result.rounds == 5
    assert _reminder_texts(result.messages) == [], "中途碰过 todo 就不该提醒"
    assert loop.todo_stall == 2


async def test_interval_zero_disables(tmp_path: Path) -> None:
    loop = _loop(
        [_echo_round("c1"), _echo_round("c2"), _text_round("done")],
        tmp_path,
        interval=0,
    )
    result = await loop.run_turn([])
    assert result.rounds == 3
    assert loop.todo_stall == 3
    assert _reminder_texts(result.messages) == []


async def test_stall_persists_across_run_turns(tmp_path: Path) -> None:
    """计数跨 run_turn 保持——跑偏发生在多条任务、很多轮之后，
    每次 send 清零等于没有 steering。

    第一次 send：2 轮 echo + 1 轮文本收尾 → 计到 3 但未触发（收尾轮顶部
    时只有 2）。第二次 send：收尾轮顶部 stall=3 ≥ 3 → 注入。
    """
    loop = _loop(
        [_echo_round("c1"), _echo_round("c2"), _text_round("done"), _text_round("again")],
        tmp_path,
        interval=3,
    )
    first = await loop.run_turn([])
    assert first.rounds == 3
    assert loop.todo_stall == 3, "第一次 send 结束后计数保留"
    assert _reminder_texts(first.messages) == [], "第一次 send 不该触发"
    second = await loop.run_turn([])
    reminders = _reminder_texts(second.messages)
    assert len(reminders) == 1, "第二次 send 应跨累计触发提醒"
    assert loop.todo_stall == 1, "第二次 send 是文本收尾轮：注入清零后本轮又计 1"


async def test_no_reminder_without_todo_tool_registered(tmp_path: Path) -> None:
    """注册表里没有 todo 工具就不提醒——否则模型会去调一个不存在的工具
    （"工具行与注册表同源"纪律的 loop 侧版本）。"""
    loop = _loop(
        [_echo_round("c1"), _echo_round("c2"), _echo_round("c3"), _text_round("done")],
        tmp_path,
        interval=3,
        with_todo=False,
    )
    result = await loop.run_turn([])
    assert result.rounds == 4
    assert _reminder_texts(result.messages) == []


async def test_reminder_enters_history_via_produced(tmp_path: Path) -> None:
    """提醒必须出现在 TurnResult.messages 里——它随后被 sdk 追加回会话树，
    恢复会话后模型仍能看到"曾被提醒过"。且提醒不在序列末尾：
    后面还有模型对提醒的响应。"""
    loop = _loop(
        [_echo_round("c1"), _echo_round("c2"), _echo_round("c3"), _text_round("done")],
        tmp_path,
        interval=3,
    )
    result = await loop.run_turn([])
    messages = result.messages
    index = next(
        (i for i, m in enumerate(messages) if _reminder_texts([m])), None
    )
    assert index is not None, "提醒必须被包含在产出消息里"
    assert index < len(messages) - 1, "提醒不在消息序列末尾（后面还有模型的响应）"
