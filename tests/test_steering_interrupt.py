"""中途打断与 steering/follow-up 双队列的门槛测试(P3-批次2 下半场):G108–G110。

G108 steering:用户补充在**下一轮模型调用前**注入,树上持久化,模型看得到。
G109 打断:InterruptToken 触发 → 协作式停在检查点(TurnCancelled),已持久化
   的部分完好,下一次 send 自动接上(断点重续);显式固定信号时 interrupt 无操作。
G110 follow-up:队列基本语义(精确排队/逐条弹出);自动执行由 REPL 循环负责,
   在 tests/test_sigma_repl.py 的会话级用例里验证。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from sigma.agent.messages import LlmMessageWrapper, convert_to_llm
from sigma.tools.base import BaseTool
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.registry import ToolRegistry
from sigma.agent.types import ToolContext, ToolResult
from sigma.providers.base import NeverCancelled, TurnCancelled
from sigma.providers.events import TextDelta
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock, UserMessage
from sigma.providers.stamps import from_epoch as ts
from sigma.sdk import InteractiveSession

FIXED_TIME = ts(1_700_000_000)


# ---------------------------------------------------------------------------
# 用具
# ---------------------------------------------------------------------------


class EchoParams(BaseModel):
    message: str = Field(description="message")


class EchoTool(BaseTool):
    name = "echo"
    description = "回显"
    read_only = True

    def __init__(self, on_run: Any = None) -> None:
        self.on_run = on_run

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        if self.on_run is not None:
            self.on_run()
        return ToolResult(content=[TextBlock(text="echo: hi")])


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _call_round(arguments: str, *, call_id: str = "call_1") -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": call_id,
            "name": "echo",
            "arguments_delta": arguments,
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _echo_registry(tool: EchoTool | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool if tool is not None else EchoTool())
    return registry


def _session(
    provider: FakeProvider, tmp_path: Path, *, signal: Any = None
) -> InteractiveSession:
    return InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        registry=_echo_registry(),
        system_prompt="测试",
        enable_compaction=False,
        enable_todo=False,
        signal=signal,
    )


def _roles_with_text(session: InteractiveSession) -> list[tuple[str, str]]:
    roles: list[tuple[str, str]] = []
    for message in session.context.history():
        if isinstance(message, LlmMessageWrapper):
            role = "user" if isinstance(message.message, UserMessage) else "assistant"
            roles.append((role, str(message.message.content)))
    return roles


# ---------------------------------------------------------------------------
# G108:steering 注入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steering_is_injected_before_next_model_call_and_persisted(
    tmp_path: Path,
) -> None:
    """轮开始时注入用户补充:树上次序 = 用户任务 → steering → assistant。"""
    session = _session(
        FakeProvider.from_rounds([_text_round("收到,按方案B"), _text_round("完成")]),
        tmp_path,
    )
    session.submit_steering("补充:改用方案B")

    await session.send("干活")

    roles = _roles_with_text(session)
    assert roles[0][0] == "user" and "干活" in roles[0][1]
    assert roles[1][0] == "user" and "方案B" in roles[1][1]
    assert roles[2][0] == "assistant"
    # 模型的请求载荷里也能看到
    llm_messages = convert_to_llm(session.context.build_messages())
    assert any(
        getattr(m, "role", "") == "user" and "方案B" in str(getattr(m, "content", ""))
        for m in llm_messages
    )


@pytest.mark.asyncio
async def test_steering_reaches_loop_via_drain() -> None:
    """loop 级:steering_drain 回调的产出在轮开始进入 produced。"""
    drained = [
        LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="先读配置再改", timestamp=FIXED_TIME),
        )
    ]
    registry = ToolRegistry()
    registry.register(EchoTool())
    def _drain() -> list[Any]:
        out = drained[:]
        drained.clear()
        return out

    loop = AgentLoop(
        provider=FakeProvider.from_rounds([_text_round("好")]),
        registry=registry,
        model="fake",
        workspace_root=".",
        signal=NeverCancelled(),
        clock=lambda: FIXED_TIME,
        steering_drain=_drain,
    )

    result = await loop.run_turn([])

    assert result.status == "completed"
    assert any(
        isinstance(m, LlmMessageWrapper)
        and isinstance(m.message, UserMessage)
        and "先读配置" in str(m.message.content)
        for m in result.messages
    )
    assert drained == []  # drain 取走即清空


# ---------------------------------------------------------------------------
# G109:打断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_stops_turn_and_next_send_resumes(tmp_path: Path) -> None:
    """工具执行中触发打断 → 下一轮检查点抛 TurnCancelled;已持久化部分完好;
    下一次 send 新令牌,断点续跑完成。"""
    session_ref: dict[str, InteractiveSession] = {}

    def cancel_from_tool() -> None:
        session_ref["session"].interrupt()

    tool = EchoTool(on_run=cancel_from_tool)
    session = InteractiveSession(
        provider=FakeProvider.from_rounds(
            [
                _call_round('{"message": "hi"}'),
                _text_round("不该出现的收尾"),
                _text_round("续跑完成"),
            ]
        ),
        workspace_root=tmp_path,
        model="fake",
        registry=_echo_registry(tool),
        system_prompt="测试",
        enable_compaction=False,
        enable_todo=False,
    )
    session_ref["session"] = session

    with pytest.raises(TurnCancelled):
        await session.send("干活")

    history_texts = str(session.context.history())
    assert "不该出现的收尾" not in history_texts  # 被打断的轮没有产出

    result = await session.send("继续")
    assert result.status == "completed"
    assert result.text == "不该出现的收尾"  # 从检查点之后继续


@pytest.mark.asyncio
async def test_interrupt_mid_stream_raises_from_provider(tmp_path: Path) -> None:
    """流中途打断:令牌在第一块之后被取消 → TurnCancelled 从流里冒出。"""

    class _SelfCancellingProvider(FakeProvider):
        """第一个文本块之后取消**自己收到的那个信号**——
        即 send 为本轮新建的 InterruptToken,与 REPL 外部打断同一通路。"""

        async def stream(self, messages: Any, tools: Any, **kwargs: Any) -> Any:
            signal = kwargs.get("signal")
            async for event in super().stream(messages, tools, **kwargs):
                if isinstance(event, TextDelta) and signal is not None:
                    signal.cancel()
                yield event

    provider = _SelfCancellingProvider.from_rounds([_text_round("很长")])
    session = InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        registry=_echo_registry(),
        system_prompt="测试",
        enable_compaction=False,
        enable_todo=False,
    )

    with pytest.raises(TurnCancelled):
        await session.send("干活")


@pytest.mark.asyncio
async def test_fixed_signal_makes_interrupt_a_noop(tmp_path: Path) -> None:
    """显式传入固定信号(评测的 NeverCancelled)→ interrupt() 永远无操作。"""
    session = _session(
        FakeProvider.from_rounds([_text_round("跑完")]),
        tmp_path,
        signal=NeverCancelled(),
    )
    assert session.interrupt() is False  # 无在跑令牌
    result = await session.send("任务")
    assert result.status == "completed"
    assert session.interrupt() is False  # 固定信号:即便有轮在跑也无法打断


# ---------------------------------------------------------------------------
# G110:follow-up 队列语义
# ---------------------------------------------------------------------------


def test_followup_queue_basics(tmp_path: Path) -> None:
    session = _session(FakeProvider.from_rounds([_text_round("ok")]), tmp_path)
    assert session.has_followups() is False
    session.submit_followup("第二条")
    session.submit_followup("第三条")
    assert session.has_followups() is True
    assert session.pop_followup() == "第二条"
    assert session.pop_followup() == "第三条"
    assert session.has_followups() is False


def test_edit_queued_followup_and_steering(tmp_path: Path) -> None:
    """edit_queued(工作台"排队项可编辑"):按显示下标改写排队文本;
    越界返回 False 不抛(与 drop_queued 同族);steering 改写只换 content。"""
    session = _session(FakeProvider.from_rounds([_text_round("ok")]), tmp_path)
    session.submit_followup("旧任务")
    assert session.edit_queued(kind="followup", index=0, text="改好的任务") is True
    assert session.pop_followup() == "改好的任务"
    assert session.edit_queued(kind="followup", index=3, text="越界") is False
    session.submit_steering("旧指导")
    assert session.edit_queued(kind="steering", index=0, text="新指导") is True
    drained = session._drain_steering()
    assert [wrapper.message.content for wrapper in drained] == ["新指导"]
