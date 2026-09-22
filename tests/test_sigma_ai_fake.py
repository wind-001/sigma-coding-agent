"""门槛 G4：``FakeProvider`` 的确定性回放。

**先说清楚这个文件测不出什么**

    G4 断言「同一 transcript 两次回放逐字节一致」。它验证的是
    ``FakeProvider`` **自身**的一致性——它读 JSONL、按顺序吐事件，
    两次当然一致。**这条断言天然会成立。**

    所以本文件**不构成"provider 层已正确"的证据**。它的实际作用是：

    1. 把"回放器是确定的"这件事**钉成回归测试**——
       将来给 ``FakeProvider`` 加功能（比如支持取消、支持错误注入）时，
       一旦引入不确定行为，这里会红。**防的是将来，不是现在。**
    2. 守住几条**有真实失败模式**的边界：轮次耗尽必须抛错而不是吐空流、
       未知事件类型必须报错而不是静默跳过、取消必须真的中断。

    回放器是否"忠实于真实 provider"，只能由 ``OpenAICompatProvider``
    与真实 API 的对照验证（详规第 9 节 R3）。

对应 ``docs/plans/P1-批次1-详规.md`` 第 5 节门槛 G4。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sigma_ai.base import CancelToken
from sigma_ai.errors import ErrorCode
from sigma_ai.events import (
    ErrorEvent,
    StopEvent,
    TextDelta,
    ToolCallDelta,
    UsageEvent,
)
from sigma_ai.fake import FakeProvider, TranscriptExhausted, load_transcript
from sigma_ai.messages import UserMessage

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


from sigma_ai.stamps import from_epoch as ts
class _NeverCancelled(CancelToken):
    """永不取消。回放测试的默认信号。"""

    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return


class _CancelledAfter(CancelToken):
    """前 N 次检查放行，之后取消。

    用它验证 ``raise_if_cancelled`` **真的被调用**——
    如果 ``FakeProvider`` 忘了在循环里检查，这个 token 永远不会触发。
    """

    def __init__(self, allow: int) -> None:
        self._allow = allow
        self._checks = 0

    def is_cancelled(self) -> bool:
        return self._checks >= self._allow

    def raise_if_cancelled(self) -> None:
        self._checks += 1
        if self._checks > self._allow:
            raise KeyboardInterrupt("cancelled")


def _round_text(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _dump(events: list[Any]) -> str:
    """把一个事件列表压成可比对的字符串。

    用 ``model_dump()`` 而不是 ``repr``：``repr`` 在字段顺序变化时结果会变，
    而字段顺序变化不影响行为——那会制造假失败。
    """
    return json.dumps(
        [event.model_dump() for event in events], ensure_ascii=False, sort_keys=True
    )


async def _drain(
    provider: FakeProvider, messages: list[Any] | None = None
) -> list[Any]:
    msgs = messages if messages is not None else [
        UserMessage(content="hi", timestamp=ts(1))
    ]
    return [
        event
        async for event in provider.stream(
            msgs, [], model="fake", signal=_NeverCancelled()
        )
    ]


# ---------------------------------------------------------------------------
# G4：确定性
# ---------------------------------------------------------------------------


async def test_same_transcript_replays_identically() -> None:
    """G4 本体：同一 transcript 两次回放，事件序列**逐字节一致**。

    注意断言的是"两次独立构造的 provider 得到相同结果"，
    而不是"同一个 provider 调用两次"——后者因为游标前进，本来就不同。
    """
    rounds = [
        _round_text("第一轮"),
        [
            {"type": "tool_call_delta", "index": 0, "id": "c1", "name": "read"},
            {"type": "tool_call_delta", "index": 0, "arguments_delta": '{"p":1}'},
            {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            {"type": "stop", "stop_reason": "tool_use"},
        ],
    ]

    first = await _drain(FakeProvider.from_rounds(rounds))
    second = await _drain(FakeProvider.from_rounds(rounds))

    assert _dump(first) == _dump(second)


async def test_replay_is_stable_after_jsonl_roundtrip(tmp_path: Path) -> None:
    """G4 的强化：走一遍**真实的文件读写**后仍然一致。

    内存里一致说明不了什么——``load_transcript`` 的解析路径
    （编码、空行、字段顺序）才是容易出问题的地方。
    """
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"events": _round_text("line one")}, ensure_ascii=False),
                "",  # 空行必须被跳过
                json.dumps({"events": _round_text("line two")}, ensure_ascii=False),
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_transcript(path)
    # 空行必须被跳过：3 行文本里只有 2 行是轮次。
    # 若空行被当成一轮，这里会是 3，而回放时第三轮会吐出一个空事件流。
    assert len(loaded) == 2

    # 同一条路径，两个独立 provider —— 逐字节一致
    first = await _drain(FakeProvider.from_file(path))
    second = await _drain(FakeProvider.from_file(path))
    assert _dump(first) == _dump(second)

    # 逐轮消费：每次调用吐一轮，两轮都真的在
    provider = FakeProvider.from_file(path)
    round1 = await _drain(provider)
    round2 = await _drain(provider)

    assert [e.text for e in round1 if isinstance(e, TextDelta)] == ["line one"]
    assert [e.text for e in round2 if isinstance(e, TextDelta)] == ["line two"]
    assert isinstance(round2[-1], StopEvent)
    assert provider.remaining_rounds == 0


# ---------------------------------------------------------------------------
# 轮次耗尽的失败模式
# ---------------------------------------------------------------------------


async def test_exhausted_transcript_raises_instead_of_returning_empty() -> None:
    """轮次用完后必须抛 ``TranscriptExhausted``，**不得静默返回空流**。

    为什么这条是真实失败模式
        静默的空响应会让 agent loop 的测试**假通过**——
        loop 正常走完、没有任何异常，看起来一切正常，
        实际上整轮对话根本没拿到数据。这类假通过极难定位。
    """
    provider = FakeProvider.from_rounds([_round_text("only one")])

    await _drain(provider)  # 第一轮正常消费

    with pytest.raises(TranscriptExhausted):
        await _drain(provider)


async def test_remaining_rounds_is_observable() -> None:
    """``remaining_rounds`` 要能反映消费进度。

    它的用途是让测试断言"transcript 被完整消费"——
    没消费完就结束，通常意味着 loop 提前退出了。
    """
    provider = FakeProvider.from_rounds([_round_text("a"), _round_text("b")])
    assert provider.remaining_rounds == 2

    await _drain(provider)
    assert provider.remaining_rounds == 1


# ---------------------------------------------------------------------------
# 未知事件类型：报错而不是静默跳过
# ---------------------------------------------------------------------------


async def test_unknown_event_type_raises() -> None:
    """transcript 里出现未知事件类型必须报错。

    与架构方案 4.0.5 节「不照抄静默丢弃」同源：静默跳过会让
    "transcript 录错了"表现成"模型少说了一句话"。
    """
    provider = FakeProvider.from_rounds(
        [[{"type": "no_such_event", "text": "x"}]]
    )
    with pytest.raises(ValueError, match="未知事件类型"):
        await _drain(provider)


async def test_missing_type_field_raises() -> None:
    """连 ``type`` 字段都没有的条目同样不能静默跳过。"""
    provider = FakeProvider.from_rounds([[{"text": "no type here"}]])
    with pytest.raises(ValueError, match="未知事件类型"):
        await _drain(provider)


# ---------------------------------------------------------------------------
# transcript 解析的健壮性
# ---------------------------------------------------------------------------


def test_load_transcript_rejects_invalid_json(tmp_path: Path) -> None:
    """坏 JSON 必须报错，并指出**行号**。

    没有行号的报错在长 transcript 上等于没有报错。
    """
    path = tmp_path / "bad.jsonl"
    path.write_text('{"events": []}\n{not json}\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r":2"):
        load_transcript(path)


def test_load_transcript_requires_events_key(tmp_path: Path) -> None:
    """缺少 ``events`` 字段的行必须报错。

    否则会安静地得到 0 个事件的轮次——回放时"这一轮什么都没吐"，
    在 loop 里表现成"模型返回了空消息"。
    """
    path = tmp_path / "no_events.jsonl"
    path.write_text('{"rounds": []}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="events"):
        load_transcript(path)


# ---------------------------------------------------------------------------
# 事件构造：各类型都要能正确还原（回放的输入面）
# ---------------------------------------------------------------------------


async def test_all_event_kinds_are_reconstructed() -> None:
    """六类事件都能从 transcript 还原，且落到正确的类上。

    这是回放的**输入面**——构造错了，回放出去的东西就是错的。
    """
    provider = FakeProvider.from_rounds(
        [
            [
                {"type": "text_delta", "text": "hi"},
                {"type": "thinking_delta", "thinking": "hmm"},
                {"type": "tool_call_delta", "index": 0, "name": "read"},
                {
                    "type": "usage",
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
                {"type": "error", "error": {"code": "rate_limit", "message": "slow"}},
                {"type": "stop", "stop_reason": "stop"},
            ]
        ]
    )
    events = await _drain(provider)

    assert [type(e).__name__ for e in events] == [
        "TextDelta",
        "ThinkingDelta",
        "ToolCallDelta",
        "UsageEvent",
        "ErrorEvent",
        "StopEvent",
    ]

    assert isinstance(events[0], TextDelta)
    assert isinstance(events[2], ToolCallDelta)
    assert isinstance(events[3], UsageEvent)
    assert events[3].usage.completion_tokens == 2
    assert isinstance(events[4], ErrorEvent)
    assert events[4].error.code is ErrorCode.RATE_LIMIT
    assert isinstance(events[5], StopEvent)


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


async def test_cancel_is_actually_checked() -> None:
    """取消信号必须**真的**在循环里被检查。

    如果 ``FakeProvider`` 只是接收了 ``signal`` 却不查，这里会全量吐完
    而不中断——取消能力就是摆设。测试必须能证伪这一点。
    """
    provider = FakeProvider.from_rounds(
        [
            [
                {"type": "text_delta", "text": "a"},
                {"type": "text_delta", "text": "b"},
                {"type": "text_delta", "text": "c"},
                {"type": "stop", "stop_reason": "stop"},
            ]
        ]
    )

    collected: list[Any] = []
    with pytest.raises(KeyboardInterrupt):
        async for event in provider.stream(
            [UserMessage(content="hi", timestamp=ts(1))],
            [],
            model="fake",
            signal=_CancelledAfter(allow=2),
        ):
            collected.append(event)

    # 取消后不得继续吐事件：只拿到前 2 个
    assert len(collected) == 2


async def test_signature_params_are_accepted_but_ignored() -> None:
    """``FakeProvider`` 必须能接受 ``sampling`` / ``options`` / ``timeout_s``。

    这条**不是**在测回放器——它测的是：签名里这三个参数存在，
    且回放器不因它们报错。
    这正是 G10「签名不能只被一个实现者适配」的另一面：
    ``FakeProvider`` 对它们**无感**（这正是它会掩盖签名缺漏的原因），
    但至少要能接住。
    """
    from sigma_ai.base import SamplingParams, StreamOptions

    provider = FakeProvider.from_rounds([_round_text("x")])
    events = [
        event
        async for event in provider.stream(
            [UserMessage(content="hi", timestamp=ts(1))],
            [],
            model="fake",
            signal=_NeverCancelled(),
            sampling=SamplingParams(temperature=0.0, max_tokens=64),
            options=StreamOptions(include_usage=True),
            timeout_s=5.0,
        )
    ]
    assert len(events) == 2
