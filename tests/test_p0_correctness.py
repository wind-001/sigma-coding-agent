"""P0 缺陷修复的门槛测试(Review-2026-09-26)。

G100 协议配对不变量:任何一轮之后,发给模型的载荷里每个 ``tool_result``
的 ``tool_call_id`` 必须能在前文 assistant 的 ``tool_calls`` 里找到——
孤儿 id 会被 OpenAI 兼容协议拒掉(400)。

G101 context_overflow 恢复:错误码结构化 → 修复悬空 → 强制压缩一次 →
重跑一次;压不了就如实报错,**不无限循环**。

G102 轮前悬空修复:错误轮留下的未应答 tool_calls 不得毒死下一轮 send。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from sigma.sdk import InteractiveSession
from sigma_agent.agent_messages import (
    LlmMessageWrapper,
    ToolResultAgentMessage,
    convert_to_llm,
)
from sigma_agent.base import BaseTool
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.base import BaseProvider, SamplingParams
from sigma_ai.errors import ErrorCode, ProviderErrorPayload
from sigma_ai.events import ErrorEvent, StopEvent, TextDelta, ToolCallDelta, UsageEvent
from sigma_ai.messages import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    Usage,
    UserMessage,
)
from sigma_ai.stamps import from_epoch as ts

FIXED_TIME = ts(1_700_000_000)


# ---------------------------------------------------------------------------
# 测试用具
# ---------------------------------------------------------------------------


class EchoParams(BaseModel):
    message: str = Field(description="要回显的内容")


class EchoTool(BaseTool):
    name = "echo"
    description = "回显 message"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            content=[TextBlock(text=f"echo: {cast_message(args)}")]
        )


def cast_message(args: BaseModel) -> str:
    return str(getattr(args, "message"))


class _ScriptedProvider(BaseProvider):
    """按**调用次数**吐预置事件轮:第 1 次请求给 rounds[0],第 2 次给 rounds[1]……

    调用次数本身是断言的一部分:溢出恢复的序列必须是
    "溢出轮 → 压缩摘要请求 → 重跑轮",多一次少一次都不对。
    超出脚本后重复最后一轮——非预期的额外请求会拿到错位的内容而失败,
    而不是静默通过。
    """

    def __init__(self, rounds: list[list[Any]]) -> None:
        self._rounds = rounds
        self.calls = 0

    async def stream(  # type: ignore[override]
        self,
        messages: list[Any],
        tools: list[dict[str, Any]],
        *,
        model: str,
        signal: Any,
        sampling: SamplingParams | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        index = min(self.calls, len(self._rounds)) - 1
        for event in self._rounds[index]:
            yield event

    def estimate_tokens(self, messages: list[Any]) -> int:
        return 10


def _text_round(text: str) -> list[Any]:
    return [TextDelta(text=text, text_signature=None), StopEvent(stop_reason="stop")]


def _summary_round(text: str) -> list[Any]:
    return [
        TextDelta(text=text, text_signature=None),
        UsageEvent(usage=Usage(prompt_tokens=20, completion_tokens=8, cached_tokens=0)),
        StopEvent(stop_reason="stop"),
    ]


def _overflow_event() -> ErrorEvent:
    return ErrorEvent(
        error=ProviderErrorPayload(
            code=ErrorCode.CONTEXT_OVERFLOW, message="prompt is too long"
        )
    )


def _make_session(provider: BaseProvider, tmp_path: Path) -> InteractiveSession:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        registry=registry,
        system_prompt="测试",
        max_rounds=6,
        enable_compaction=True,
        enable_todo=False,
    )


def _prefill(session: InteractiveSession, rounds: int) -> None:
    """灌入 rounds 轮旧历史(每轮 user+assistant)。

    压缩需要"可压的段":keep_recent_rounds=4,历史至少要有 5 个轮次
    才压得出东西——G101 的恢复路径依赖这一点。
    """
    for index in range(rounds):
        session.context.append(
            LlmMessageWrapper(
                timestamp=FIXED_TIME,
                message=UserMessage(content=f"旧任务 {index}", timestamp=FIXED_TIME),
            ),
            LlmMessageWrapper(
                timestamp=FIXED_TIME,
                message=AssistantMessage(
                    content=[TextBlock(text=f"旧回答 {index}")],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
                    stop_reason="stop",
                    timestamp=FIXED_TIME,
                ),
            ),
        )


def _assert_results_paired(messages: list[Any]) -> None:
    """G100 的不变量:tool_result 的 id 必须出现在前文 assistant 的 tool_calls 里。"""
    open_ids: set[str] = set()
    for message in messages:
        role = getattr(message, "role", "")
        if role == "assistant":
            open_ids |= {
                block.id
                for block in message.content
                if isinstance(block, ToolCallBlock)
            }
        elif role == "tool_result":
            assert message.tool_call_id in open_ids, (
                f"孤儿 tool_result:{message.tool_call_id!r}"
                "(不在任何前文 assistant 的 tool_calls 里——协议 400 风险)"
            )


# ---------------------------------------------------------------------------
# G100:unparsed 失败调用 → user 注记,协议配对不破
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unparsed_call_becomes_user_note_and_keeps_protocol_paired(
    tmp_path: Path,
) -> None:
    """拼装失败(JSON 截断)的调用:不产生孤儿 tool_result,模型看得到注记。

    注入:恢复旧的合成 tool_call_id="unparsed_{index}" 路径 → 配对断言红。
    """
    provider = _ScriptedProvider(
        [
            # 第 1 轮:一个 JSON 截断的调用(拼装失败)
            [
                ToolCallDelta(
                    index=0,
                    id="call_bad",
                    name="echo",
                    arguments_delta='{"message": "hi"',
                ),
                StopEvent(stop_reason="tool_use"),
            ],
            _text_round("好,重来了"),
        ]
    )
    session = _make_session(provider, tmp_path)

    result = await session.send("干活")

    assert result.status == "completed"
    history = session.context.history()
    # 模型必须看得到"调用没有被接受"
    notes = [
        m
        for m in history
        if isinstance(m, LlmMessageWrapper)
        and isinstance(m.message, UserMessage)
        and "无法解析" in str(m.message.content)
    ]
    assert len(notes) == 1
    # assistant 不携带失败调用块(不伪装成合法调用)
    assistants = [
        m.message
        for m in history
        if isinstance(m, LlmMessageWrapper) and isinstance(m.message, AssistantMessage)
    ]
    assert all(
        not any(isinstance(b, ToolCallBlock) for b in a.content) for a in assistants
    )
    # 协议配对不变量
    _assert_results_paired(convert_to_llm(session.context.build_messages()))


# ---------------------------------------------------------------------------
# G101:context_overflow → 修复 + 强压 + 重跑一次;压不了如实报错
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_overflow_triggers_compaction_and_retry(tmp_path: Path) -> None:
    """溢出轮 → 压缩摘要请求 → 重跑完成;调用次数 3 = 恢复序列的形状。"""
    provider = _ScriptedProvider(
        [
            [_overflow_event()],
            _summary_round("旧任务摘要:完成了 6 个旧步骤。"),
            _text_round("恢复后完成"),
        ]
    )
    session = _make_session(provider, tmp_path)
    _prefill(session, 6)

    result = await session.send("触发溢出")

    assert result.status == "completed"
    assert result.error_code is None
    assert provider.calls == 3
    assert session.last_compaction is not None


@pytest.mark.asyncio
async def test_overflow_without_compactable_history_surfaces_error(
    tmp_path: Path,
) -> None:
    """没有可压的段(历史太短)→ 原样返回 error,不重跑、不无限循环。"""
    provider = _ScriptedProvider(
        [
            [_overflow_event()],
            _text_round("不该被消费的一轮"),
        ]
    )
    session = _make_session(provider, tmp_path)

    result = await session.send("触发溢出")

    assert result.status == "error"
    assert result.error_code == "context_overflow"
    assert provider.calls == 1


# ---------------------------------------------------------------------------
# G102:错误轮留下的悬空 tool_calls,不得毒死下一轮 send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dangling_calls_from_errored_turn_are_repaired_next_send(
    tmp_path: Path,
) -> None:
    """轮 1 在批次执行前溢出 → assistant 已持久化、结果悬空;
    轮 2 的 send 自动补齐合成结果,配对不变量保持。

    注入:删掉 _attempt_turn 开头的轮前修复 → 轮 2 的载荷带悬空
    assistant,G100 断言红。
    """
    provider = _ScriptedProvider(
        [
            # 轮 1:拼装成功的调用 + 溢出错误(无 stop)→ assistant 持久化但结果悬空
            [
                TextDelta(text="先看文件", text_signature=None),
                ToolCallDelta(
                    index=0,
                    id="call_1",
                    name="echo",
                    arguments_delta='{"message": "hi"}',
                ),
                _overflow_event(),
            ],
            _text_round("恢复完成"),
        ]
    )
    session = _make_session(provider, tmp_path)

    first = await session.send("干活")
    assert first.status == "error"
    assert first.error_code == "context_overflow"

    second = await session.send("继续")
    assert second.status == "completed"

    repaired = [
        m
        for m in session.context.history()
        if isinstance(m, ToolResultAgentMessage) and m.tool_call_id == "call_1"
    ]
    assert len(repaired) == 1
    assert repaired[0].is_error is True
    assert repaired[0].details.get("repaired") is True

    _assert_results_paired(convert_to_llm(session.context.build_messages()))
