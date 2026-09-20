"""确定性回放 Provider：agent loop 离线测试的地基。

为什么必须有它
    架构方案 7.2 节把「确定性回放」列为核心设计之一：
    agent loop 的测试要**离线且确定**——不联网、不需要 API key、
    同样的输入两次跑出逐字节一样的结果。没有这个，loop 的测试会变成
    "偶尔通过"，而偶尔通过的测试等于没有测试。

**关于 G4 门槛的一条重要说明（诚实的边界）**
    G4 断言「同一 transcript 两次回放产出逐字节一致」。
    这条断言**验证的是本类自身的一致性**——它读 JSONL、按顺序吐事件，
    两次当然一致。**它天然会成立。**

    所以**不能把 G4 当作"回放器正确"的证据**。它真正的作用是：
    给 agent loop 的测试提供一个**稳定的输入源**。
    回放器是否"忠实于真实 provider"，只能由 ``OpenAICompatProvider``
    与真实 API 的对照来验证（详规第 9 节 R3）。

    **这条边界写在这里，防止它被误读成"provider 层已验证"。**

transcript 格式
    JSONL，每行一个「轮次」，形如::

        {"events": [{"type": "text_delta", "text": "hello"}, ...], "usage": {...}}

    轮次按顺序被消费；用完后继续调用会抛 ``StopIteration``
    ——**不静默返回空**，因为静默的空响应会让 loop 测试出现难排查的假通过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sigma_ai.base import BaseProvider
from sigma_ai.tokens import estimate_messages

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sigma_ai.base import CancelToken, SamplingParams, StreamOptions
    from sigma_ai.events import StreamEvent
    from sigma_ai.messages import LlmMessage


class TranscriptExhausted(RuntimeError):
    """transcript 的轮次已用完，但仍在请求。

    **故意抛错而不是返回空流**：静默的空响应会让 agent loop 的测试
    出现"假通过"——loop 正常结束，看起来没问题，实际上根本没拿到数据。
    """


def load_transcript(path: Path) -> list[list[dict[str, Any]]]:
    """读取 JSONL transcript。

    每行是一个轮次，含 ``events`` 列表。空行被跳过。
    """
    rounds: list[list[dict[str, Any]]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_no} 不是合法 JSON：{exc}"
                ) from exc
            if "events" not in payload:
                raise ValueError(f"{path}:{line_no} 缺少 'events' 字段")
            rounds.append(list(payload["events"]))
    return rounds


class FakeProvider(BaseProvider):
    """按 transcript 顺序吐事件的 provider。

    用法::

        provider = FakeProvider.from_file(Path("transcripts/read_then_edit.jsonl"))
        async for event in provider.stream(messages, [], model="fake", signal=token):
            ...

    本类**忽略** ``sampling`` / ``options`` / ``timeout_s`` 三个参数——
    这正是它作为"第二实现者"的局限：它无法验证签名是否够用。
    那是 ``OpenAICompatProvider`` 的职责（门槛 G10）。
    """

    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        self._rounds = rounds
        self._cursor = 0

    @classmethod
    def from_file(cls, path: Path) -> FakeProvider:
        return cls(load_transcript(path))

    @classmethod
    def from_rounds(cls, rounds: list[list[dict[str, Any]]]) -> FakeProvider:
        """直接从内存构造，方便单测。"""
        return cls(rounds)

    @property
    def remaining_rounds(self) -> int:
        """还剩几个轮次。测试用它断言"transcript 被完整消费"。"""
        return max(0, len(self._rounds) - self._cursor)

    def stream(
        self,
        messages: list[LlmMessage],
        tools: list[dict[str, Any]],
        *,
        model: str,
        signal: CancelToken,
        sampling: SamplingParams | None = None,
        options: StreamOptions | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """回放下一轮事件。

        实现是 ``async def`` 套 ``yield`` 的异步生成器——
        注意不能写成"返回一个生成器"，否则调用方 ``async for`` 会拿到
        coroutine 而不是迭代器。
        """
        return self._stream_round(messages, model, signal)

    async def _stream_round(
        self,
        messages: list[LlmMessage],
        model: str,
        signal: CancelToken,
    ) -> AsyncIterator[StreamEvent]:
        from sigma_ai.events import (
            ErrorEvent,
            StopEvent,
            TextDelta,
            ThinkingDelta,
            ToolCallDelta,
            UsageEvent,
        )

        if self._cursor >= len(self._rounds):
            raise TranscriptExhausted(
                f"transcript 只有 {len(self._rounds)} 轮，但已请求第 "
                f"{self._cursor + 1} 轮"
            )

        raw_events = self._rounds[self._cursor]
        self._cursor += 1

        # 事件类型 → 构造器 的显式映射。
        # 不用 getattr(module, name) 这类动态查找：认不出的类型必须报错，
        # 而不是静默跳过（与 4.0.5 节「不照抄静默丢弃」同源）。
        builders: dict[str, Any] = {
            "text_delta": TextDelta,
            "thinking_delta": ThinkingDelta,
            "tool_call_delta": ToolCallDelta,
            "usage": UsageEvent,
            "stop": StopEvent,
            "error": ErrorEvent,
        }

        for raw in raw_events:
            signal.raise_if_cancelled()

            event_type = raw.get("type")
            if event_type not in builders:
                raise ValueError(
                    f"transcript 里出现未知事件类型：{event_type!r}。"
                    f"已知类型：{sorted(builders)}"
                )
            yield builders[event_type](**raw)

    def estimate_tokens(self, messages: list[LlmMessage]) -> int:
        """回放场景下按常规方式估算——与真实 provider 行为一致。"""
        return estimate_messages(messages)
