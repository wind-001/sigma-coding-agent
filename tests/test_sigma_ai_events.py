"""门槛 G3：六类 ``StreamEvent`` 逐类 round-trip。

为什么事件必须能序列化
    事件流是 **UI / 落盘（transcript）/ 测试** 三方共用的契约
    （``events.py`` 模块 docstring）。只要有一类事件不能 round-trip，
    确定性回放就地失效——而回放是整个 agent loop 测试的地基。

本文件是 1c 的测试，先前只有 ``errors.py`` 的 payload 往返被覆盖，
六类事件本身**没有一条针对性的断言**。这是补上的缺口。

对应 ``docs/plans/P1-批次1-详规.md`` 第 5 节门槛 G3。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from sigma_ai.errors import ErrorCode, ProviderErrorPayload
from sigma_ai.events import (
    ErrorEvent,
    StopEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageEvent,
)
from sigma_ai.messages import Usage

# 每个样例都必须带"最不容易存活"的字段，否则 round-trip 测了等于没测。
#
# 具体说：``*_signature`` 全是可选字段，默认 None。若样例不带签名，
# 即使序列化把签名整个丢掉，测试照样全绿。
# 所以每类事件的样例都要显式塞一个签名进去。
_ROUNDTRIP_SAMPLES: list[Any] = [
    TextDelta(text="hello", text_signature="SIG-TEXT"),
    ThinkingDelta(thinking="hmm", thinking_signature="SIG-THINK"),
    ToolCallDelta(
        index=2,
        id="call_abc",
        name="read",
        arguments_delta='{"path": "a.py"}',
        thought_signature="SIG-TOOL",
    ),
    UsageEvent(
        usage=Usage(prompt_tokens=1200, completion_tokens=34, cached_tokens=1024)
    ),
    StopEvent(stop_reason="tool_use"),
    ErrorEvent(
        error=ProviderErrorPayload(
            code=ErrorCode.CONTEXT_OVERFLOW,
            message="prompt is too long",
            raw={"error": {"message": "prompt is too long"}},
            status_code=400,
        )
    ),
]


@pytest.mark.parametrize(
    "event", _ROUNDTRIP_SAMPLES, ids=[type(e).__name__ for e in _ROUNDTRIP_SAMPLES]
)
def test_stream_event_roundtrip_by_class(event: Any) -> None:
    """G3：每类事件两次往返后**完全相等**。

    断言用 ``model_dump()`` 的**全字段相等**而不是逐字段 ``assert``：
    逐字段写会漏掉"新加的字段没人测"——而新加字段恰恰是最容易在
    序列化里丢的东西。全字段相等对新增字段自动生效。
    """
    cls = type(event)
    once = cls.model_validate_json(event.model_dump_json())
    twice = cls.model_validate_json(once.model_dump_json())

    assert once.model_dump() == event.model_dump()
    assert twice.model_dump() == event.model_dump()


def test_text_delta_signature_none_survives() -> None:
    """G3 的反面：没有签名时不得被填成 ``""``。

    ``None`` 与 ``""`` 在协议上不是一回事——某些 provider 会拒绝空签名串。
    把 ``None`` 序列化成 ``""`` 会在多轮对话里产生难定位的异常。
    """
    back = TextDelta.model_validate_json(TextDelta(text="x").model_dump_json())
    assert back.text_signature is None


def test_tool_call_delta_index_is_required() -> None:
    """``index`` 必须是必填字段。

    它用于多工具并行时的分片归属（详规 2.7 节坑第 4 条）。
    给它默认值会让"忘了填 index"静默变成"所有分片都归到第 0 个工具"，
    症状是工具参数被拼成一堆乱码——**而不报警**。
    """
    with pytest.raises(ValidationError):
        ToolCallDelta()  # type: ignore[call-arg]


def test_error_event_holds_payload_not_exception() -> None:
    """``ErrorEvent.error`` 必须是**可序列化的数据**，不是异常实例。

    这条断言对应实施时发现的真实缺陷：原设计让 ``ErrorEvent`` 直接持有
    ``ProviderError``（Exception），Pydantic 无法为它生成 schema。
    修法不是开 ``arbitrary_types_allowed``（那只是让报错消失，序列化仍丢
    ``code`` / ``raw``），而是拆成 payload + 异常两个类型。
    """
    payload = ProviderErrorPayload(code=ErrorCode.RATE_LIMIT, message="slow down")
    event = ErrorEvent(error=payload)

    back = ErrorEvent.model_validate_json(event.model_dump_json())
    assert isinstance(back.error, ProviderErrorPayload)
    assert back.error.code is ErrorCode.RATE_LIMIT
    # 关键：往返后仍然可判断"该不该重试"。丢了 code 这条信息就没了。
    assert back.error.retriable is True


def test_event_type_discriminant_is_exact() -> None:
    """每类事件的 ``type`` 字面量与类名一一对应，不允许重名。

    transcript 回放靠 ``type`` 找构造器（``fake.py``），
    两个类共用同一个 ``type`` 值会让回放静默构造出错误的类型。
    """
    pairs = [(type(event).__name__, event.type) for event in _ROUNDTRIP_SAMPLES]
    type_values = [value for _, value in pairs]
    assert len(set(type_values)) == len(type_values), f"type 值有重复：{pairs}"
