"""门槛 G-ANTH-1..8：``AnthropicProvider`` 的离线单测（详规 §6）。

**这个文件在批次的定位**

    Anthropic Messages API 是 ``BaseProvider`` 的第二个真实协议实现。
    全部测试走 ``httpx.MockTransport`` / 合成 SSE 行——**零真实联网调用**
    （与 ``test_sigma_ai_openai_compat.py`` 同一款测试替身，同一款纪律）。

    八道门槛的分布：

    - G-ANTH-1 消息映射 → convert 节（system 顶层化 / tool_result 归 user
      合并 / assistant tool_use 形状 / 交替违反即抛）
    - G-ANTH-2 SSE 分帧与增量拼装 → sse 节 + 端到端节
    - G-ANTH-3 usage 映射含 cached；缺 usage 不炸 → protocol 节 + 端到端节
    - G-ANTH-4 stop_reason 四值映射逐行钉住 → protocol 节
    - G-ANTH-5 错误归一化五分支；payload 可 roundtrip → protocol 节
    - G-ANTH-6 断流检测（无 message_stop 即截断）→ 端到端节
    - G-ANTH-7 工具 schema 形状 + 多工具并行 index 归属 → convert 节 + 端到端节
    - G-ANTH-8 registry protocol 字段 + CLI 分派 → registry 节

**这个文件证明不了什么**

    fixture 是我按 Anthropic 官方文档的 SSE 事件序合成的，真实端点的
    字段出入（cache 字段位置、message_delta 的 usage 形状）未被验证——
    全绿不代表可以不加复核地真连 api.anthropic.com。
"""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from typing import Any

import httpx
import pytest
from sigma.providers.anthropic import AnthropicProvider
from sigma.providers.anthropic.convert import (
    DroppedThinkingBlock,
    messages_to_anthropic,
    tools_to_anthropic,
)
from sigma.providers.anthropic.protocol import (
    UnmappedStopReason,
    _error_from_response,
    _error_from_stream_event,
    _map_stop_reason,
    _parse_usage,
)
from sigma.providers.anthropic.sse import SseEventParser
from sigma.providers.base import BaseProvider, CancelToken, SamplingParams
from sigma.providers.errors import ErrorCode, ProviderError, ProviderErrorPayload
from sigma.providers.events import (
    ErrorEvent,
    StopEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageEvent,
)
from sigma.providers.messages import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from sigma.providers.openai import OpenAICompatProvider
from sigma.providers.registry import ProviderSpec, builtin_providers
from sigma.providers.stamps import from_epoch as ts
from sigma.providers.tool_calls import ToolCallAssembler

# ---------------------------------------------------------------------------
# 测试替身（与 openai_compat 测试同款：MockTransport 只换掉网络那一跳）
# ---------------------------------------------------------------------------


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return


def _frame(event: str, payload: dict[str, Any]) -> str:
    """把一个 Anthropic 事件拼成 SSE 帧（``event:`` 行 + ``data:`` 行 + 空行）。"""
    return (
        f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _sse(*events: str) -> bytes:
    """把若干帧拼成响应体。"""
    return "".join(events).encode("utf-8")


def _text_stream_events(
    texts: list[str],
    *,
    stop_reason: str = "end_turn",
    usage_in: dict[str, Any] | None = None,
    usage_out: dict[str, Any] | None = None,
    with_stop: bool = True,
) -> list[str]:
    """合成一条"纯文本"事件序：message_start → 增量 → message_delta → message_stop。"""
    start_usage = usage_in if usage_in is not None else {"input_tokens": 12}
    frames = [
        _frame(
            "message_start",
            {"type": "message_start", "message": {"usage": start_usage}},
        )
    ]
    for text in texts:
        frames.append(
            _frame(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            )
        )
        frames.append(
            _frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
    frames.append(
        _frame("content_block_stop", {"type": "content_block_stop", "index": 0})
    )
    frames.append(
        _frame(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason},
                "usage": usage_out if usage_out is not None else {"output_tokens": 2},
            },
        )
    )
    if with_stop:
        frames.append(_frame("message_stop", {"type": "message_stop"}))
    return frames


class _Recorder:
    """记录最后一次请求的 URL / headers / body，供断言使用。"""

    def __init__(self, response_body: bytes, status: int = 200) -> None:
        self.body = response_body
        self.status = status
        self.last_request: httpx.Request | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return httpx.Response(
            self.status,
            content=self.body,
            headers={"content-type": "text/event-stream"},
        )


def _provider(
    recorder: _Recorder | None = None, body: bytes | None = None
) -> tuple[AnthropicProvider, _Recorder]:
    """默认响应体是一条**完整的最小流**（含 message_stop）——
    只查请求体形状的用例不该顺带收到截断错误。"""
    rec = recorder if recorder is not None else _Recorder(
        body if body is not None else _sse(*_text_stream_events(["ok"]))
    )
    transport = httpx.MockTransport(rec.handler)
    client = httpx.AsyncClient(transport=transport)
    provider = AnthropicProvider(
        base_url="https://example.invalid",
        api_key="sk-ant-test",
        client=client,
    )
    return provider, rec


async def _collect(provider: AnthropicProvider, **kwargs: Any) -> list[Any]:
    messages = kwargs.pop(
        "messages", [UserMessage(content="hi", timestamp=ts(1))]
    )
    return [
        event
        async for event in provider.stream(
            messages,
            kwargs.pop("tools", []),
            model="test-model",
            signal=_NeverCancelled(),
            **kwargs,
        )
    ]


# ---------------------------------------------------------------------------
# G-ANTH-1：消息映射（convert.py）
# ---------------------------------------------------------------------------


def test_system_message_becomes_top_level_field() -> None:
    """system 是**顶层参数**，不进 messages 数组——与 OpenAI 的结构差异 #1。"""
    conv = messages_to_anthropic(
        [
            SystemMessage(content="you are helpful", timestamp=ts(1)),
            UserMessage(content="hi", timestamp=ts(2)),
        ]
    )
    assert conv.system == "you are helpful"
    assert conv.messages == [{"role": "user", "content": "hi"}]
    # messages 里不得再出现 system role
    assert all(entry["role"] != "system" for entry in conv.messages)


def test_multiple_system_messages_raise() -> None:
    """P1 设计保证 system 只有一条（架构 4.0.4）——多条即 ``ValueError``，宁可崩不要错。"""
    with pytest.raises(ValueError, match="system"):
        messages_to_anthropic(
            [
                SystemMessage(content="a", timestamp=ts(1)),
                SystemMessage(content="b", timestamp=ts(2)),
                UserMessage(content="hi", timestamp=ts(3)),
            ]
        )


def test_system_message_block_content() -> None:
    """system 的块形态 → Anthropic 的块数组；单个文本块降级成字符串。"""
    conv = messages_to_anthropic(
        [
            SystemMessage(
                content=[TextBlock(text="a"), TextBlock(text="b")],
                timestamp=ts(1),
            ),
            UserMessage(content="hi", timestamp=ts(2)),
        ]
    )
    assert conv.system == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]

    conv_single = messages_to_anthropic(
        [
            SystemMessage(content=[TextBlock(text="only")], timestamp=ts(1)),
            UserMessage(content="hi", timestamp=ts(2)),
        ]
    )
    assert conv_single.system == "only"


def test_tool_result_becomes_user_tool_result_block() -> None:
    """tool_result 是 **user 消息里的 content block**，不是独立 role——结构差异 #2。"""
    conv = messages_to_anthropic(
        [
            UserMessage(content="hi", timestamp=ts(1)),
            AssistantMessage(
                content=[
                    ToolCallBlock(id="call_1", name="read", arguments={"path": "a.py"})
                ],
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                stop_reason="tool_use",
                timestamp=ts(2),
            ),
            ToolResultMessage(
                tool_call_id="call_1",
                tool_name="read",
                content=[TextBlock(text="file contents")],
                timestamp=ts(3),
            ),
        ]
    )
    assert conv.messages[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": [{"type": "text", "text": "file contents"}],
                "is_error": False,
            }
        ],
    }


def test_consecutive_tool_results_merge_into_one_user_message() -> None:
    """连续的 tool_result 合并进**同一条** user 消息——结构差异 #2 的后半句。"""
    conv = messages_to_anthropic(
        [
            UserMessage(content="hi", timestamp=ts(1)),
            AssistantMessage(
                content=[
                    ToolCallBlock(id="c1", name="read", arguments={}),
                    ToolCallBlock(id="c2", name="bash", arguments={}),
                ],
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                stop_reason="tool_use",
                timestamp=ts(2),
            ),
            ToolResultMessage(
                tool_call_id="c1",
                tool_name="read",
                content=[TextBlock(text="r1")],
                timestamp=ts(3),
            ),
            ToolResultMessage(
                tool_call_id="c2",
                tool_name="bash",
                content=[TextBlock(text="r2")],
                is_error=True,
                timestamp=ts(4),
            ),
        ]
    )
    assert len(conv.messages) == 3  # user / assistant / 合并后的 user
    merged = conv.messages[2]
    assert merged["role"] == "user"
    assert len(merged["content"]) == 2
    assert merged["content"][0]["tool_use_id"] == "c1"
    assert merged["content"][1]["tool_use_id"] == "c2"
    assert merged["content"][1]["is_error"] is True


def test_tool_result_rejects_multimodal_content() -> None:
    """工具结果的 content 暂不支持多模态——**报错而不是静默降级**（与 openai 同口径）。"""
    from sigma.providers.messages import ImageBlock

    with pytest.raises(TypeError, match="多模态"):
        messages_to_anthropic(
            [
                ToolResultMessage(
                    tool_call_id="c1",
                    tool_name="screenshot",
                    content=[ImageBlock(data="aGk=", mime_type="image/png")],
                    timestamp=ts(1),
                )
            ]
        )


def test_assistant_tool_call_becomes_tool_use_block() -> None:
    """``ToolCallBlock`` → ``tool_use`` 块，且 ``input`` 是**对象**不是 JSON 串——结构差异 #3。"""
    conv = messages_to_anthropic(
        [
            UserMessage(content="hi", timestamp=ts(1)),
            AssistantMessage(
                content=[
                    ToolCallBlock(
                        id="call_1", name="read", arguments={"path": "a.py", "lines": 10}
                    )
                ],
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                stop_reason="tool_use",
                timestamp=ts(2),
            ),
        ]
    )
    assistant = conv.messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "a.py", "lines": 10}}
    ]
    # input 必须是对象——json.dumps 后再发字符串是 Anthropic 最常见的接错方式
    assert isinstance(assistant["content"][0]["input"], dict)


def test_assistant_text_and_tool_use_coexist() -> None:
    """文本与工具调用同时存在时**都保留**，且按原顺序排列。"""
    conv = messages_to_anthropic(
        [
            UserMessage(content="hi", timestamp=ts(1)),
            AssistantMessage(
                content=[
                    TextBlock(text="let me look"),
                    ToolCallBlock(id="c1", name="read", arguments={}),
                ],
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                stop_reason="tool_use",
                timestamp=ts(2),
            ),
        ]
    )
    content = conv.messages[1]["content"]
    assert content[0] == {"type": "text", "text": "let me look"}
    assert content[1]["type"] == "tool_use"


def test_thinking_block_dropped_but_counted_visibly() -> None:
    """thinking 块 v1 **丢弃并计数可见**（详规 §3）——防御性丢弃必须留痕。

    同时钉住透传保真的另一半：转换**不得改动输入消息**——原消息上的
    ``thinking_signature`` 必须原样还在（架构 4.0 纪律：签名不丢）。
    """
    msg = AssistantMessage(
        content=[
            ThinkingBlock(thinking="secret", thinking_signature="SIG"),
            TextBlock(text="answer"),
        ],
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="stop",
        timestamp=ts(1),
    )
    with pytest.warns(DroppedThinkingBlock, match="thinking"):
        conv = messages_to_anthropic([UserMessage(content="hi", timestamp=ts(0)), msg])

    assert conv.dropped_thinking_blocks == 1
    assert conv.messages[1]["content"] == [{"type": "text", "text": "answer"}]
    assert "secret" not in json.dumps(conv.messages)
    # 输入未被改动：签名还在原消息上
    assert msg.content[0].thinking_signature == "SIG"  # type: ignore[union-attr]


def test_assistant_with_only_thinking_raises() -> None:
    """丢弃 thinking 后 content 为空的 assistant 消息——宁可崩（Anthropic 拒收空 content）。"""
    msg = AssistantMessage(
        content=[ThinkingBlock(thinking="only")],
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="stop",
        timestamp=ts(1),
    )
    with pytest.warns(DroppedThinkingBlock):
        with pytest.raises(ValueError, match="thinking"):
            messages_to_anthropic(
                [UserMessage(content="hi", timestamp=ts(0)), msg]
            )


def test_alternation_violation_raises() -> None:
    """消息序列必须严格 user/assistant 交替——违反即 ``ValueError``，列出 role 序列。"""
    with pytest.raises(ValueError, match="交替"):
        messages_to_anthropic(
            [
                UserMessage(content="a", timestamp=ts(1)),
                UserMessage(content="b", timestamp=ts(2)),
            ]
        )
    with pytest.raises(ValueError, match="交替"):
        messages_to_anthropic(
            [
                UserMessage(content="a", timestamp=ts(1)),
                AssistantMessage(
                    content=[TextBlock(text="x")],
                    usage=Usage(prompt_tokens=1, completion_tokens=1),
                    stop_reason="stop",
                    timestamp=ts(2),
                ),
                AssistantMessage(
                    content=[TextBlock(text="y")],
                    usage=Usage(prompt_tokens=1, completion_tokens=1),
                    stop_reason="stop",
                    timestamp=ts(3),
                ),
            ]
        )


def test_unknown_message_type_raises() -> None:
    """认不出的消息类型必须报错，不得静默丢弃或当 user 发出去。"""

    class _Fake:
        role = "user"

    with pytest.raises(ValueError, match="未知的 LLM 消息类型"):
        messages_to_anthropic([_Fake()])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# G-ANTH-7（前半）：工具 schema 形状（convert.py）
# ---------------------------------------------------------------------------


def test_tools_converted_to_anthropic_shape() -> None:
    """OpenAI 形状（``function.parameters``）→ Anthropic 形状（``input_schema`` 直挂）。"""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    assert tools_to_anthropic(tools) == [
        {
            "name": "read",
            "description": "read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


def test_tools_with_unknown_shape_raise() -> None:
    """认不出的工具定义形状必须报错——不静默丢弃工具。"""
    with pytest.raises(ValueError, match="工具"):
        tools_to_anthropic([{"type": "function", "name": "bare"}])


# ---------------------------------------------------------------------------
# G-ANTH-3：usage 映射（protocol.py）
# ---------------------------------------------------------------------------


def test_parse_usage_maps_anthropic_fields() -> None:
    """prompt=input_tokens / completion=output_tokens / cached=cache_read_input_tokens。"""
    usage = _parse_usage(
        {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 64}
    )
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 20
    assert usage.cached_tokens == 64


def test_parse_usage_missing_fields_default_to_zero() -> None:
    """缺 usage / 缺字段**不炸**：全按 0 计。"""
    usage = _parse_usage({})
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.cached_tokens == 0


def test_parse_usage_null_fields() -> None:
    """字段显式为 ``null`` 时按 0 计（``or 0`` 防线，与 openai 侧同款）。"""
    usage = _parse_usage({"input_tokens": None, "output_tokens": None})
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0


# ---------------------------------------------------------------------------
# G-ANTH-4：stop_reason 四值映射（protocol.py）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("tool_use", "tool_use"),
        ("max_tokens", "length"),
    ],
)
def test_map_stop_reason_known_values(raw: str, expected: str) -> None:
    """四值逐行钉住（详规 §4 的映射表）。"""
    assert _map_stop_reason(raw) == expected


def test_map_stop_reason_warns_on_unknown() -> None:
    """未收录的取值必须**告警**，不能静默当成 stop（与 openai 侧同一条纪律）。"""
    with pytest.warns(UnmappedStopReason, match="未收录"):
        assert _map_stop_reason("some_new_reason") == "stop"


# ---------------------------------------------------------------------------
# G-ANTH-5：错误归一化五分支 + payload roundtrip（protocol.py）
# ---------------------------------------------------------------------------


def test_error_429_is_rate_limit() -> None:
    body = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})
    payload = _error_from_response(429, body)
    assert payload.code is ErrorCode.RATE_LIMIT
    assert payload.retriable is True
    assert payload.status_code == 429


def test_error_overloaded_is_transient() -> None:
    """``overloaded_error``（Anthropic 的 529）→ transient——可重试。"""
    body = json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}})
    payload = _error_from_response(529, body)
    assert payload.code is ErrorCode.TRANSIENT
    assert payload.retriable is True

    # 错误类型是权威判据：状态码缺位时按 type 归类
    payload_by_type = _error_from_response(500, body)
    assert payload_by_type.code is ErrorCode.TRANSIENT


def test_error_invalid_request_with_overflow_text_is_context_overflow() -> None:
    """``invalid_request_error`` + 超限文案 → ``context_overflow``（压缩触发路径之一）。"""
    body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "prompt is too long: 200000 tokens > 180000 maximum",
            },
        }
    )
    payload = _error_from_response(400, body)
    assert payload.code is ErrorCode.CONTEXT_OVERFLOW
    assert payload.retriable is False


def test_error_auth() -> None:
    """认证失败 → ``auth``——重试无意义。"""
    body = json.dumps(
        {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    )
    payload = _error_from_response(401, body)
    assert payload.code is ErrorCode.AUTH


def test_error_fallback_transient() -> None:
    """其余（如 ``api_error``）→ ``transient``——详规 §4 的第五分支。"""
    body = json.dumps({"type": "error", "error": {"type": "api_error", "message": "internal"}})
    payload = _error_from_response(500, body)
    assert payload.code is ErrorCode.TRANSIENT


def test_error_falls_back_to_raw_text() -> None:
    """不是 JSON 时保留原文——厂商差异排查没线索就废了。"""
    payload = _error_from_response(503, "<html>upstream unavailable</html>")
    assert payload.code is ErrorCode.TRANSIENT
    assert "upstream unavailable" in payload.message


def test_error_payload_roundtrip() -> None:
    """``ProviderErrorPayload`` 是 pydantic 模型——落盘再读回，逐字段相等。"""
    body = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})
    payload = _error_from_response(429, body)

    restored = ProviderErrorPayload.model_validate(payload.model_dump())
    assert restored == payload

    exc = ProviderError.from_payload(payload)
    assert exc.code is ErrorCode.RATE_LIMIT
    assert exc.status_code == 429


def test_error_from_stream_event() -> None:
    """流中途的 ``error`` 事件没有状态码——按错误类型归类。"""
    payload = _error_from_stream_event(
        {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}}
    )
    assert payload.code is ErrorCode.TRANSIENT
    assert payload.status_code is None
    assert payload.raw["error"]["type"] == "overloaded_error"


# ---------------------------------------------------------------------------
# G-ANTH-2：SSE 分帧（sse.py）
# ---------------------------------------------------------------------------


def test_sse_parser_pairs_event_and_data_lines() -> None:
    """``event:`` 类型行与下一个 ``data:`` 行配对——Anthropic 比 OpenAI 多的一层。"""
    parser = SseEventParser()
    assert parser.feed("event: content_block_delta") is None
    ev = parser.feed('data: {"type": "content_block_delta", "delta": {}}')
    assert ev is not None
    assert ev.event == "content_block_delta"
    assert ev.data == {"type": "content_block_delta", "delta": {}}


def test_sse_parser_falls_back_to_event_line() -> None:
    """data JSON 缺 ``type`` 字段时，回退用 ``event:`` 行的值。"""
    parser = SseEventParser()
    parser.feed("event: ping")
    ev = parser.feed('data: {"x": 1}')
    assert ev is not None
    assert ev.event == "ping"


def test_sse_parser_ignores_non_data_lines() -> None:
    """注释行 / 空行 / ``id:`` ``retry:`` 字段一律忽略，不报错。"""
    parser = SseEventParser()
    assert parser.feed("") is None
    assert parser.feed(": keep-alive") is None
    assert parser.feed("id: 3") is None
    assert parser.feed("retry: 100") is None


def test_sse_parser_raises_on_broken_json() -> None:
    """半截 JSON 必须**抛出**，由 provider 记成错误事件（不静默跳过）。"""
    parser = SseEventParser()
    parser.feed("event: content_block_delta")
    with pytest.raises(json.JSONDecodeError):
        parser.feed('data: {"type": "content_block_del')


def test_sse_parser_event_name_does_not_leak_across_blank_lines() -> None:
    """悬空的 ``event:`` 行在空行后失效——不得串到后面的数据行。"""
    parser = SseEventParser()
    parser.feed("event: orphan")
    parser.feed("")
    ev = parser.feed('data: {"type": "message_stop"}')
    assert ev is not None
    assert ev.event == "message_stop"


# ---------------------------------------------------------------------------
# 端到端（MockTransport）：请求体 + 事件流
# ---------------------------------------------------------------------------


async def test_request_url_headers_and_body_shape() -> None:
    """URL 拼接、``x-api-key`` + ``anthropic-version`` 头、``max_tokens`` 必填。"""
    provider, rec = _provider()
    await _collect(provider)

    assert rec.last_request is not None
    assert str(rec.last_request.url) == "https://example.invalid/v1/messages"
    assert rec.last_request.headers["x-api-key"] == "sk-ant-test"
    assert rec.last_request.headers["anthropic-version"] == "2023-06-01"

    body = json.loads(rec.last_request.content)
    assert body["model"] == "test-model"
    assert body["stream"] is True
    # max_tokens 是 **Anthropic 协议必填**（无默认值）——缺省 4096 是协议兜底
    assert body["max_tokens"] == 4096
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "system" not in body  # 没有 system 消息就不发
    assert "tools" not in body  # 没有工具就不发（空数组行为不确定）


async def test_request_max_tokens_from_sampling() -> None:
    """上层给了 ``SamplingParams.max_tokens`` 就用它；temperature/top_p 只发非 None。"""
    from sigma.providers.base import SamplingParams

    provider, rec = _provider()
    await _collect(
        provider, sampling=SamplingParams(temperature=0.2, max_tokens=256)
    )
    assert rec.last_request is not None
    body = json.loads(rec.last_request.content)
    assert body["max_tokens"] == 256
    assert body["temperature"] == 0.2
    assert "top_p" not in body


async def test_request_system_and_tools_shape() -> None:
    """system 顶层化与工具形状在**请求体层面**的落地（G-ANTH-1/7 的端到端断言）。"""
    from sigma.providers.base import SamplingParams  # noqa: F401

    provider, rec = _provider()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    messages = [
        SystemMessage(content="you are helpful", timestamp=ts(0)),
        UserMessage(content="hi", timestamp=ts(1)),
    ]
    await _collect(provider, messages=messages, tools=tools)

    assert rec.last_request is not None
    body = json.loads(rec.last_request.content)
    assert body["system"] == "you are helpful"
    assert body["tools"] == [
        {"name": "read", "description": "read a file", "input_schema": {"type": "object", "properties": {}}}
    ]
    assert [entry["role"] for entry in body["messages"]] == ["user"]


async def test_stream_produces_expected_event_sequence() -> None:
    """完整流：文本增量 → usage → stop，顺序与内容都要对（G-ANTH-2/3）。"""
    body = _sse(*_text_stream_events(["你", "好"]))
    provider, _ = _provider(body=body)
    events = await _collect(provider)

    texts = [e for e in events if isinstance(e, TextDelta)]
    assert [e.text for e in texts] == ["你", "好"]

    usages = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usages) == 1
    assert usages[0].usage.prompt_tokens == 12
    assert usages[0].usage.completion_tokens == 2
    assert usages[0].usage.cached_tokens == 0

    stops = [e for e in events if isinstance(e, StopEvent)]
    assert len(stops) == 1
    assert stops[0].stop_reason == "stop"
    assert isinstance(events[-1], StopEvent)  # StopEvent 是流的最后一个事件


async def test_stream_usage_with_cache_read() -> None:
    """cached 来自 message_start 的 ``cache_read_input_tokens``（G-ANTH-3 的 cached 部分）。"""
    body = _sse(
        *_text_stream_events(
            ["ok"],
            usage_in={"input_tokens": 100, "cache_read_input_tokens": 80},
            usage_out={"output_tokens": 5},
        )
    )
    provider, _ = _provider(body=body)
    events = await _collect(provider)

    usage = next(e for e in events if isinstance(e, UsageEvent)).usage
    assert usage.prompt_tokens == 100
    assert usage.cached_tokens == 80
    assert usage.completion_tokens == 5


async def test_stream_without_usage_does_not_crash() -> None:
    """message_start / message_delta 都缺 usage 时**不炸**：没有 UsageEvent，StopEvent 照常。"""
    frames = [
        _frame("message_start", {"type": "message_start", "message": {}}),
        _frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
        ),
        _frame("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        _frame("message_stop", {"type": "message_stop"}),
    ]
    provider, _ = _provider(body=_sse(*frames))
    events = await _collect(provider)

    assert not [e for e in events if isinstance(e, UsageEvent)]
    assert isinstance(events[-1], StopEvent)


async def test_thinking_delta_yields_thinking_event() -> None:
    """``thinking_delta`` → ``ThinkingDelta``（事件类型既有，不新增）。"""
    frames = [
        _frame("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "hmm"},
            },
        ),
        _frame("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        _frame("message_stop", {"type": "message_stop"}),
    ]
    provider, _ = _provider(body=_sse(*frames))
    events = await _collect(provider)

    thinking = [e for e in events if isinstance(e, ThinkingDelta)]
    assert [e.thinking for e in thinking] == ["hmm"]
    assert isinstance(events[-1], StopEvent)


# ---------------------------------------------------------------------------
# G-ANTH-7（后半）：tool_use 块的分片与 index 归属
# ---------------------------------------------------------------------------


def _tool_use_frames() -> list[str]:
    """单工具：content_block_start(tool_use) → 两段 input_json_delta → 收尾。"""
    return [
        _frame("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}),
        _frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read"},
            },
        ),
        _frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"pa'},
            },
        ),
        _frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": 'th": "a.py"}'},
            },
        ),
        _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _frame(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
        ),
        _frame("message_stop", {"type": "message_stop"}),
    ]


async def test_tool_use_fragments_assemble_to_complete_json() -> None:
    """``input_json_delta`` 分片 → 上层装配出**完整 JSON 参数**（G-ANTH-2 后半）。"""
    provider, _ = _provider(body=_sse(*_tool_use_frames()))
    events = await _collect(provider)

    deltas = [e for e in events if isinstance(e, ToolCallDelta)]
    assert [e.arguments_delta for e in deltas] == ["{\"pa", "th\": \"a.py\"}"]
    # 第一个分片带 id/name（来自 content_block_start），后续分片不带
    assert deltas[0].id == "toolu_1"
    assert deltas[0].name == "read"
    assert deltas[1].id is None
    assert deltas[1].name is None

    assembler = ToolCallAssembler()
    for delta in deltas:
        assembler.feed(delta)
    (call,) = assembler.finish()
    assert call.ok
    assert call.arguments == {"path": "a.py"}

    stop = next(e for e in events if isinstance(e, StopEvent))
    assert stop.stop_reason == "tool_use"


async def test_parallel_tool_calls_index_attribution() -> None:
    """多工具并行：两个 index 的分片**交错到达**，归属必须正确（G-ANTH-7）。"""
    frames = [
        _frame("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}),
        _frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_a", "name": "read"},
            },
        ),
        _frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_b", "name": "bash"},
            },
        ),
        # 交错：0 → 1 → 1 → 0
        _frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a'}},
        ),
        _frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"b'}},
        ),
        _frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '": 2}'}},
        ),
        _frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '": 1}'}},
        ),
        _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _frame("content_block_stop", {"type": "content_block_stop", "index": 1}),
        _frame(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
        ),
        _frame("message_stop", {"type": "message_stop"}),
    ]
    provider, _ = _provider(body=_sse(*frames))
    events = await _collect(provider)

    assembler = ToolCallAssembler()
    for delta in (e for e in events if isinstance(e, ToolCallDelta)):
        assembler.feed(delta)
    calls = assembler.finish()  # finish 按 index 升序
    assert [c.arguments for c in calls] == [{"a": 1}, {"b": 2}]
    assert [c.name for c in calls] == ["read", "bash"]
    assert [c.id for c in calls] == ["toolu_a", "toolu_b"]


async def test_tool_use_without_deltas_still_yields_one_fragment() -> None:
    """``content_block_start(tool_use)`` 后零分片也要补一个空 ``ToolCallDelta``——
    否则这个调用会在上层**无声消失**（空参数是合法边界，不是数据缺失）。"""
    frames = [
        _frame("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_e", "name": "ping"},
            },
        ),
        _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _frame(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}},
        ),
        _frame("message_stop", {"type": "message_stop"}),
    ]
    provider, _ = _provider(body=_sse(*frames))
    events = await _collect(provider)

    deltas = [e for e in events if isinstance(e, ToolCallDelta)]
    assert len(deltas) == 1
    assert deltas[0].id == "toolu_e"
    assert deltas[0].name == "ping"
    assert deltas[0].arguments_delta == ""


# ---------------------------------------------------------------------------
# G-ANTH-6：断流检测 + 错误即事件
# ---------------------------------------------------------------------------


async def test_eof_without_message_stop_is_truncation_error() -> None:
    """无 ``message_stop`` 就 EOF = **被截断**，必须可见（与 openai 侧 saw_finish 同口径）。

    已产出的部分（TextDelta）必须保留；流以错误收尾，不得补发 StopEvent。
    """
    body = _sse(*_text_stream_events(["half"], with_stop=False))
    provider, _ = _provider(body=body)
    events = await _collect(provider)

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["half"]
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].error.code is ErrorCode.TRANSIENT
    assert "截断" in errors[0].error.message
    assert not [e for e in events if isinstance(e, StopEvent)]


async def test_http_error_becomes_error_event_not_exception() -> None:
    """HTTP 错误必须变成 ``ErrorEvent``，不得抛异常打断流（G-ANTH-5 的端到端）。"""
    body = json.dumps(
        {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit exceeded"}}
    ).encode()
    provider, _ = _provider(_Recorder(body, status=429))
    events = await _collect(provider)

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].error.code is ErrorCode.RATE_LIMIT
    assert events[0].error.retriable is True


async def test_midstream_error_event_becomes_error_event() -> None:
    """流中途的 ``error`` 事件 → 归一化的 ``ErrorEvent``，且不再发 StopEvent。"""
    frames = [
        _frame("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "before"}},
        ),
        _frame(
            "error",
            {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}},
        ),
    ]
    provider, _ = _provider(body=_sse(*frames))
    events = await _collect(provider)

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["before"]  # 已产出的部分保留
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].error.code is ErrorCode.TRANSIENT
    assert not [e for e in events if isinstance(e, StopEvent)]


async def test_broken_sse_line_does_not_kill_the_stream() -> None:
    """一行坏 JSON 只记一条错误事件，后续内容**继续吐**（与 openai 同一折中）。"""
    frames = [
        _frame("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "before"}},
        ),
        "data: {\"type\": \"content_block_delta\", \"index\": 0, \"delta\": {\"type\": \"text_delta\", \"broken\"\n\n",
        _frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "after"}},
        ),
        _frame("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        _frame("message_stop", {"type": "message_stop"}),
    ]
    provider, _ = _provider(body=_sse(*frames))
    events = await _collect(provider)

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["before", "after"]  # 坏行没吞掉前后内容
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].error.code is ErrorCode.INVALID_REQUEST
    assert isinstance(events[-1], StopEvent)


async def test_non_dict_sse_data_becomes_error_event() -> None:
    """``data: 123``（合法 JSON 但不是对象）→ ErrorEvent，不裸抛。"""
    frames = [
        _frame("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        "data: 123\n\n",
        _frame("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        _frame("message_stop", {"type": "message_stop"}),
    ]
    provider, _ = _provider(body=_sse(*frames))
    events = await _collect(provider)

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].error.code is ErrorCode.INVALID_REQUEST
    assert "不是 JSON 对象" in errors[0].error.message
    assert isinstance(events[-1], StopEvent)


async def test_ping_events_are_ignored() -> None:
    """``ping``（保活）与 ``content_block_stop`` 不产生事件，也不能打断流。"""
    frames = [
        _frame("ping", {"type": "ping"}),
        *_text_stream_events(["ok"]),
        _frame("ping", {"type": "ping"}),
    ]
    provider, _ = _provider(body=_sse(*frames))
    events = await _collect(provider)

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["ok"]
    assert isinstance(events[-1], StopEvent)
    assert not [e for e in events if isinstance(e, ErrorEvent)]


async def test_unknown_event_type_warns_but_stream_continues() -> None:
    """未知事件类型：**告警可见**但不中断流——丢弃必须可见，不静默。"""
    frames = [
        _frame("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _frame("future_event", {"type": "future_event", "x": 1}),
        _frame("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        _frame("message_stop", {"type": "message_stop"}),
    ]
    provider, _ = _provider(body=_sse(*frames))
    with pytest.warns(UserWarning, match="未收录"):
        events = await _collect(provider)

    assert isinstance(events[-1], StopEvent)
    assert not [e for e in events if isinstance(e, ErrorEvent)]


async def test_network_failure_becomes_transient_error() -> None:
    """网络层异常也要变成 ``TRANSIENT`` 错误事件（可重试语义）。"""

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_boom))
    provider = AnthropicProvider(base_url="https://example.invalid", client=client)
    events = await _collect(provider)

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].error.code is ErrorCode.TRANSIENT
    assert events[0].error.retriable is True


async def test_stream_is_async_iterable_not_coroutine() -> None:
    """``stream()`` 返回异步迭代器而不是 coroutine（与 openai 同一条教训）。"""
    provider, _ = _provider()
    stream = provider.stream(
        [UserMessage(content="hi", timestamp=ts(1))],
        [],
        model="m",
        signal=_NeverCancelled(),
    )
    assert hasattr(stream, "__aiter__")


async def test_external_client_is_not_closed_by_provider() -> None:
    """注入的 client 由调用方负责关闭——provider 不得擅自关掉它。"""
    provider, _ = _provider()
    await _collect(provider)
    await provider.aclose()  # 注入的 client，这里应当是 no-op

    events = await _collect(provider)
    assert isinstance(events[-1], StopEvent)


def test_estimate_tokens_matches_shared_estimator() -> None:
    """token 估算走共享实现（providers.tokens），不另起炉灶。"""
    from sigma.providers.tokens import estimate_messages

    provider = AnthropicProvider(
        base_url="https://example.invalid", client=httpx.AsyncClient()
    )
    messages = [UserMessage(content="hello world", timestamp=ts(1))]
    assert provider.estimate_tokens(messages) == estimate_messages(messages)


# ---------------------------------------------------------------------------
# G-ANTH-8：registry 与 CLI 分派
# ---------------------------------------------------------------------------


def test_builtin_anthropic_entry() -> None:
    """anthropic 条目可解析：base_url / default_model / protocol 三项都对。"""
    spec = builtin_providers().resolve("anthropic")
    assert spec.base_url == "https://api.anthropic.com"
    assert spec.default_model == "claude-sonnet-4-5"
    assert spec.protocol == "anthropic"


def test_protocol_field_defaults_keep_existing_five() -> None:
    """protocol 带默认值——既有五条 spec 零改动，位置构造仍然可用。"""
    registry = builtin_providers()
    assert len(registry) == 6
    for name in ("deepseek", "moonshot", "zhipu", "dashscope", "ollama"):
        spec = registry.resolve(name)
        assert spec.protocol == "openai-compat"

    # 三参位置构造（既有调用点形状）不因新字段破坏
    positional = ProviderSpec("x", "https://x", "m")
    assert positional.protocol == "openai-compat"


def test_make_provider_dispatches_by_protocol() -> None:
    """``_make_provider`` 按 ``spec.protocol`` 分派实现类。"""
    from sigma.cli.main import DEFAULT_PRESET, _make_provider

    anthropic_provider = _make_provider(
        Namespace(preset="anthropic"), "https://api.anthropic.com", "k"
    )
    assert isinstance(anthropic_provider, AnthropicProvider)
    assert isinstance(anthropic_provider, BaseProvider)

    openai_provider = _make_provider(
        Namespace(preset="deepseek"), "https://api.deepseek.com/v1", "k"
    )
    assert isinstance(openai_provider, OpenAICompatProvider)
    assert not isinstance(openai_provider, AnthropicProvider)

    # 无 preset（默认路径）不回归：仍走 openai 兼容实现
    default_provider = _make_provider(Namespace(preset=None), "https://x", "k")
    assert isinstance(default_provider, OpenAICompatProvider)
    assert DEFAULT_PRESET == "deepseek"


async def test_request_body_extra_body_merges_last() -> None:
    """extra_body 通道与 openai 侧同判据:thinking 预算这类协议私有参数
    由壳层拼好放进来,provider 最后合并;None 时不出现任何额外键。"""
    provider, rec = _provider()
    events = await _collect(
        provider,
        sampling=SamplingParams(extra_body={"thinking": {"type": "enabled"}}),
    )
    assert events  # 流正常走完
    body = json.loads(rec.last_request.content)
    assert body["thinking"] == {"type": "enabled"}
    assert body["model"] == "test-model"


async def test_request_body_extra_body_none_is_inert() -> None:
    provider, rec = _provider()
    await _collect(provider, sampling=SamplingParams(temperature=0.5))
    body = json.loads(rec.last_request.content)
    assert "thinking" not in body
    assert "reasoning_effort" not in body
    assert body["temperature"] == 0.5
