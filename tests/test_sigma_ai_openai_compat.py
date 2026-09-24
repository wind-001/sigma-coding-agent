"""门槛 G10 的一半：``OpenAICompatProvider`` 的离线单测。

**这个文件在批次的定位**

    ``OpenAICompatProvider`` 在批次 1 的职责**不是**"完整支持真实调用"，
    而是**"作为第二个实现者，把 ``BaseProvider`` 签名里的缺漏逼出来"**。
    所以下面这些测试的作用是双重的：

    1. 把协议层的翻译逻辑（消息转换 / SSE 解析 / usage / finish_reason）
       钉成回归测试；
    2. **证明签名真的够用**——如果 ``stream()`` 少了 ``sampling`` /
       ``options`` / ``timeout_s``，这个文件里会有一批测试**根本写不出来**。

**这个文件证明不了什么**（详规第 9 节 R1–R6）

    fixture 是我录的，边界情况是我自己挑的。所以：

    - 错误码映射是否符合各厂商的**真实**响应体 → 未验证（R2）
    - SSE 分帧在真实流上的健壮性 → 未验证（R3）
    - ``finish_reason`` 的实际取值 → 未验证（R4）
    - ``usage`` 是否真能取到 → 未验证（R5）
    - 多工具 ``index`` 归属 → 未验证（R6）

    全绿**不代表 provider 层能用**。

对应 ``docs/plans/P1-批次1-详规.md`` 第 5 节门槛 G10。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from sigma_ai.base import CancelToken, SamplingParams, StreamOptions
from sigma_ai.errors import ErrorCode
from sigma_ai.events import ErrorEvent, StopEvent, TextDelta, UsageEvent
from sigma_ai.messages import (
    AssistantMessage,
    ImageBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from sigma_ai.openai import OpenAICompatProvider
from sigma_ai.openai.convert import message_to_openai
from sigma_ai.openai.protocol import (
    UnmappedFinishReason,
    _error_from_response,
    _map_finish_reason,
    _parse_usage,
)
from sigma_ai.openai.sse import parse_sse_line

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


from sigma_ai.stamps import from_epoch as ts
class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return


def _sse(*chunks: dict[str, Any], done: bool = True) -> bytes:
    """把若干 chunk 拼成 SSE 响应体。"""
    lines = [
        f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks
    ]
    if done:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"choices": []}
    if content is not None or tool_calls is not None or finish_reason is not None:
        delta: dict[str, Any] = {}
        if content is not None:
            delta["content"] = content
        if tool_calls is not None:
            delta["tool_calls"] = tool_calls
        choice: dict[str, Any] = {"delta": delta}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        payload["choices"] = [choice]
    if usage is not None:
        payload["usage"] = usage
    return payload


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


def _provider(recorder: _Recorder | None = None, body: bytes | None = None) -> tuple[
    OpenAICompatProvider, _Recorder
]:
    """构造一个走 MockTransport 的 provider。

    ``MockTransport`` 是 httpx 自带的测试传输层——它挂在 client 上，
    所以 ``base_url`` / headers / body 全部走真实代码路径，
    只有**网络那一跳**被换掉。这正是 B1.2 拍板选它的原因。
    """
    rec = recorder if recorder is not None else _Recorder(body or _sse())
    transport = httpx.MockTransport(rec.handler)
    client = httpx.AsyncClient(transport=transport)
    provider = OpenAICompatProvider(
        base_url="https://example.invalid/v1",
        api_key="sk-test",
        client=client,
    )
    return provider, rec


async def _collect(provider: OpenAICompatProvider, **kwargs: Any) -> list[Any]:
    messages = kwargs.pop("messages", [UserMessage(content="hi", timestamp=ts(1))])
    return [
        event
        async for event in provider.stream(
            messages, kwargs.pop("tools", []), model="test-model",
            signal=_NeverCancelled(), **kwargs
        )
    ]


# ---------------------------------------------------------------------------
# 消息转换：四种消息 + 四个内容块
# ---------------------------------------------------------------------------


def test_system_message_converts_to_plain_string() -> None:
    """纯文本 system 消息降级成裸字符串（少一层嵌套、少几个 token）。"""
    msg = SystemMessage(content="you are helpful", timestamp=ts(1))
    assert message_to_openai(msg) == {"role": "system", "content": "you are helpful"}


def test_user_message_with_image_converts_to_parts() -> None:
    """带图片的 user 消息必须转成 ``content`` 数组，且是 data URL。

    图片走 base64 内联而不是 URL 引用——本地文件没有可访问 URL。
    """
    msg = UserMessage(
        content=[
            TextBlock(text="what is this"),
            ImageBlock(data="aGVsbG8=", mime_type="image/png"),
        ],
        timestamp=ts(1),
    )
    out = message_to_openai(msg)
    assert out["role"] == "user"
    assert out["content"] == [
        {"type": "text", "text": "what is this"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aGVsbG8="},
        },
    ]


def test_assistant_tool_call_becomes_tool_calls_field() -> None:
    """``ToolCallBlock`` 必须转成 ``tool_calls`` 字段，且 ``arguments`` 是 JSON 串。

    三个关键点：
    1. 有 tool_calls 时 ``content`` 置 ``None``（协议要求，不能是空串）
    2. ``arguments`` 是**序列化后的字符串**，不是 dict
    3. ``json.dumps`` 用 ``ensure_ascii=False``——中文参数不该被转成 ``\\uXXXX``
       （否则 token 数翻几倍，且日志不可读）
    """
    msg = AssistantMessage(
        content=[
            ToolCallBlock(
                id="call_1", name="read", arguments={"path": "中文.py", "lines": 10}
            )
        ],
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="tool_use",
        timestamp=ts(1),
    )
    out = message_to_openai(msg)

    assert out["role"] == "assistant"
    assert out["content"] is None
    assert out["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read", "arguments": '{"path": "中文.py", "lines": 10}'},
        }
    ]


def test_assistant_text_and_tool_call_coexist() -> None:
    """文本与工具调用可以同时存在——不能因为要发工具调用就把文本丢掉。"""
    msg = AssistantMessage(
        content=[
            TextBlock(text="let me look"),
            ToolCallBlock(id="c1", name="read", arguments={}),
        ],
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="tool_use",
        timestamp=ts(1),
    )
    out = message_to_openai(msg)
    assert out["content"] == "let me look"
    assert len(out["tool_calls"]) == 1


def test_thinking_block_is_not_sent_but_survives_locally() -> None:
    """思考块**不发给** OpenAI 兼容端点（协议里没有这个位置）。

    但它必须留在本地消息里——否则 ``thinking_signature`` 会丢，
    而签名的丢失会导致多轮对话异常且症状不指向根因（门槛 G2）。
    所以这条断言测的是"不发"，G2 测的是"不丢"，两者互补。
    """
    msg = AssistantMessage(
        content=[
            ThinkingBlock(thinking="secret reasoning", thinking_signature="SIG"),
            TextBlock(text="answer"),
        ],
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="stop",
        timestamp=ts(1),
    )
    out = message_to_openai(msg)
    assert out["content"] == "answer"  # 只剩文本，且降级成字符串
    assert "secret reasoning" not in json.dumps(out)


def test_tool_result_becomes_its_own_role() -> None:
    """工具结果是独立的 ``role="tool"`` 消息，靠 ``tool_call_id`` 关联。

    这正是"``tool_result`` 是独立角色"（架构方案 4.0.1 节）的收益：
    转换时不需要把结果塞回 assistant 消息里，少一层映射。
    """
    msg = ToolResultMessage(
        tool_call_id="call_1",
        tool_name="read",
        content=[TextBlock(text="file contents")],
        timestamp=ts(1),
    )
    out = message_to_openai(msg)
    assert out == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "file contents",
    }


def test_tool_result_rejects_multimodal_content() -> None:
    """工具结果的 content 暂不支持多模态——**必须报错而不是静默降级**。

    静默降级成 ``str(list)`` 会发出去一串没人能读的东西，
    模型收到后行为不可预期，且完全看不出是这里的问题。
    """
    msg = ToolResultMessage(
        tool_call_id="c1",
        tool_name="screenshot",
        content=[ImageBlock(data="aGk=", mime_type="image/png")],
        timestamp=ts(1),
    )
    with pytest.raises(TypeError, match="多模态"):
        message_to_openai(msg)


def test_unknown_message_type_raises() -> None:
    """认不出的消息类型必须报错，不得静默当成 user 发出去。"""

    class _Fake:
        role = "user"

    with pytest.raises(ValueError, match="未知的 LLM 消息类型"):
        message_to_openai(_Fake())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------


def test_parse_sse_line_returns_payload() -> None:
    assert parse_sse_line('data: {"a": 1}') == {"a": 1}


@pytest.mark.parametrize(
    "line",
    ["", "   ", ": keep-alive comment", "data: [DONE]", "event: message", "id: 3"],
)
def test_parse_sse_line_returns_none_for_non_data(line: str) -> None:
    """非数据行一律返回 ``None``，**不报错**。

    重点是 ``data: [DONE]``：它**不是 JSON**，送去 ``json.loads`` 会抛异常，
    而异常会被误当成"协议被破坏"。这是详规 2.7 节列的坑第 2 条。

    ``: keep-alive`` 这类注释行同理——协议允许，必须忽略。
    """
    assert parse_sse_line(line) is None


def test_parse_sse_line_raises_on_broken_json() -> None:
    """半截 JSON 必须**抛出**，由调用方决定怎么处理。

    这里不返回 ``None``：返回 ``None`` 等于"这行没有数据"，
    而实际上是"这行有数据但坏了"——两者完全不同。
    调用方（``_aiter_stream``）把它记成错误事件，不中断整个流。
    """
    with pytest.raises(json.JSONDecodeError):
        parse_sse_line('data: {"a": 1')


# ---------------------------------------------------------------------------
# usage / finish_reason / 错误分类
# ---------------------------------------------------------------------------


def test_parse_usage_openai_nested_shape() -> None:
    """OpenAI 把 cached_tokens 放在 ``prompt_tokens_details`` 里。"""
    usage = _parse_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 64},
        }
    )
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 20
    assert usage.cached_tokens == 64


def test_parse_usage_flat_shape() -> None:
    """部分兼容厂商把 cached_tokens 直接放顶层——两种都要认。

    这是兼容层存在的实际理由之一：**同一个协议名字下有真实差异**。
    """
    usage = _parse_usage(
        {"prompt_tokens": 50, "completion_tokens": 5, "cached_tokens": 48}
    )
    assert usage.cached_tokens == 48


def test_parse_usage_without_cache_info() -> None:
    """没有缓存信息时 ``cached_tokens`` 为 0，不为 None。

    prompt cache 命中率是 D4 的核心指标，必须能直接参与算术。
    """
    usage = _parse_usage({"prompt_tokens": 10, "completion_tokens": 1})
    assert usage.cached_tokens == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stop", "stop"),
        ("length", "length"),
        ("tool_calls", "tool_use"),
        ("function_call", "tool_use"),
        ("content_filter", "content_filter"),
        ("end_turn", "stop"),
        ("max_tokens", "length"),
    ],
)
def test_map_finish_reason_known_values(raw: str, expected: str) -> None:
    """各厂商的取值都要映射到统一的 ``StopReason``。

    多对一：``max_tokens`` 与 ``length`` 都是 length，
    ``function_call`` 与 ``tool_calls`` 都是 tool_use。
    """
    assert _map_finish_reason(raw) == expected


def test_map_finish_reason_warns_on_unknown() -> None:
    """未收录的取值必须**告警**，不能静默当成 stop。

    静默的后果："模型被截断"看起来像"正常结束"——
    这是评测里最危险的一类假通过。
    """
    with pytest.warns(UnmappedFinishReason, match="未收录"):
        assert _map_finish_reason("some_new_reason") == "stop"


def test_error_from_response_reads_openai_error_shape() -> None:
    """优先读 ``{"error": {"message": ...}}``，并保留原始响应体。"""
    body = json.dumps({"error": {"message": "invalid api key", "type": "auth"}})
    payload = _error_from_response(401, body)

    assert payload.code is ErrorCode.AUTH
    assert payload.message == "invalid api key"
    assert payload.status_code == 401
    assert payload.retriable is False
    # raw 必须留原文：这是自研适配层相对 SDK 的实际诊断收益
    assert payload.raw["error"]["type"] == "auth"


def test_error_from_response_falls_back_to_raw_text() -> None:
    """不是 JSON 时保留原文——否则厂商差异排查没线索。"""
    payload = _error_from_response(503, "<html>upstream unavailable</html>")
    assert payload.code is ErrorCode.TRANSIENT
    assert "upstream unavailable" in payload.message


def test_error_from_response_detects_context_overflow() -> None:
    """``400`` + 超限文案 → ``context_overflow``（门槛 G6 在网络层的落地）。"""
    body = json.dumps(
        {"error": {"message": "This model's maximum context length is 8192 tokens"}}
    )
    payload = _error_from_response(400, body)
    assert payload.code is ErrorCode.CONTEXT_OVERFLOW


# ---------------------------------------------------------------------------
# 端到端（MockTransport）：请求体 + 事件流
# ---------------------------------------------------------------------------


async def test_request_body_contains_sampling_and_stream_options() -> None:
    """**G10 的核心断言**：``sampling`` / ``options`` 真的进了请求体。

    这条测试的存在本身就是 B1.3 的产出：
    如果 ``BaseProvider.stream()`` 没有 ``sampling`` / ``options`` 两个参数，
    **这个测试根本写不出来**——你无法把 temperature 传下去。

    同时验证"不塞 null"：只有显式给过的字段才进 body。
    """
    provider, rec = _provider()

    events = await _collect(
        provider,
        sampling=SamplingParams(temperature=0.2, max_tokens=256),
        options=StreamOptions(include_usage=True),
        timeout_s=30.0,
    )
    assert isinstance(events[-1], StopEvent)

    assert rec.last_request is not None
    body = json.loads(rec.last_request.content)

    assert body["model"] == "test-model"
    assert body["stream"] is True
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 256
    assert "top_p" not in body  # 没给就不发，避免厂商把 null 当非法值
    assert body["stream_options"] == {"include_usage": True}


async def test_request_asks_for_usage_by_default() -> None:
    """不传 ``options`` 时也必须请求 usage。

    默认不请求的后果：流里没有 usage，用量统计只能靠估算——
    而估算数不能进评测报告（详规 4.1 节）。默认值错会让这个坑静默存在。
    """
    provider, rec = _provider()
    await _collect(provider)

    assert rec.last_request is not None
    body = json.loads(rec.last_request.content)
    assert body["stream_options"] == {"include_usage": True}


async def test_request_url_and_auth_header() -> None:
    """URL 拼接与鉴权头走真实代码路径（MockTransport 只换掉网络那一跳）。"""
    provider, rec = _provider()
    await _collect(provider)

    assert rec.last_request is not None
    assert str(rec.last_request.url) == "https://example.invalid/v1/chat/completions"
    assert rec.last_request.headers["authorization"] == "Bearer sk-test"


async def test_tools_are_sent_only_when_present() -> None:
    """没有工具时不得发 ``"tools": []``。

    空数组在部分实现上会被当成"给了 tools 字段但没有工具"，
    行为不确定；而且白占 token。
    """
    provider, rec = _provider()
    await _collect(provider)
    assert rec.last_request is not None
    assert "tools" not in json.loads(rec.last_request.content)

    tool = {"type": "function", "function": {"name": "read"}}
    provider2, rec2 = _provider()
    await _collect(provider2, tools=[tool])
    assert rec2.last_request is not None
    assert json.loads(rec2.last_request.content)["tools"] == [tool]


async def test_stream_produces_expected_event_sequence() -> None:
    """完整流：文本增量 → usage → stop，顺序与内容都要对。"""
    body = _sse(
        _chunk(content="你"),
        _chunk(content="好"),
        _chunk(finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 12, "completion_tokens": 2}),
    )
    provider, _ = _provider(body=body)
    events = await _collect(provider)

    texts = [e for e in events if isinstance(e, TextDelta)]
    assert [e.text for e in texts] == ["你", "好"]

    usages = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usages) == 1
    assert usages[0].usage.prompt_tokens == 12

    stops = [e for e in events if isinstance(e, StopEvent)]
    assert len(stops) == 1
    assert stops[0].stop_reason == "stop"


async def test_tool_call_fragments_are_split_and_indexed() -> None:
    """工具调用分片按 ``index`` 归属，且 ``arguments_delta`` 是**原始片段**。

    本层不拼装参数（那是 loop 的职责，批次 4）。这里只保证：
    分片按 index 归到正确的工具上、``id`` / ``name`` 在首个分片里出现、
    ``arguments_delta`` 是未解析的字符串片段。
    """
    body = _sse(
        _chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_a",
                    "function": {"name": "read", "arguments": '{"pa'},
                }
            ]
        ),
        _chunk(tool_calls=[{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]),
        _chunk(finish_reason="tool_calls"),
    )
    provider, _ = _provider(body=body)
    events = await _collect(provider)

    calls = [e for e in events if type(e).__name__ == "ToolCallDelta"]
    assert len(calls) == 2
    assert calls[0].index == 0
    assert calls[0].id == "call_a"
    assert calls[0].name == "read"
    assert calls[0].arguments_delta == '{"pa'
    assert calls[1].arguments_delta == 'th": "a.py"}'
    # 第二个分片不带 id/name，值保持 None——不得被填成 ""
    assert calls[1].id is None
    assert calls[1].name is None

    stop = next(e for e in events if isinstance(e, StopEvent))
    assert stop.stop_reason == "tool_use"


async def test_http_error_becomes_error_event_not_exception() -> None:
    """HTTP 错误必须变成 ``ErrorEvent``，**不得抛异常打断流**。

    为什么是事件不是异常：上层需要拿到"已经产出的部分"与"错误本身"两样东西。
    抛异常会丢掉前者——而流式响应到了第 90% 才失败是常态。
    """
    body = json.dumps({"error": {"message": "rate limit exceeded"}}).encode()
    provider, _ = _provider(_Recorder(body, status=429))
    events = await _collect(provider)

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].error.code is ErrorCode.RATE_LIMIT
    assert events[0].error.retriable is True


async def test_broken_sse_line_does_not_kill_the_stream() -> None:
    """一行坏 JSON 只记一条错误事件，后续内容**继续吐**。

    这是"不静默跳过、也不整个中断"的折中：
    静默跳过会让消息莫名消失；整个中断会让一处畸形数据毁掉整个响应。
    """
    raw = (
        b'data: {"choices":[{"delta":{"content":"before"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"broken\n\n'
        b'data: {"choices":[{"delta":{"content":"after"},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    provider, _ = _provider(body=raw)
    events = await _collect(provider)

    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["before", "after"]  # 坏行没吞掉前后内容

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].error.code is ErrorCode.INVALID_REQUEST
    assert isinstance(events[-1], StopEvent)


async def test_network_failure_becomes_transient_error() -> None:
    """网络层异常（连接失败、超时）也要变成 ``TRANSIENT`` 错误事件。

    否则一次网络抖动会让整个 agent loop 崩掉，
    而这个错误本来是**可重试**的（``retriable=True``）。
    """
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_boom))
    provider = OpenAICompatProvider(
        base_url="https://example.invalid/v1", client=client
    )
    events = await _collect(provider)

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].error.code is ErrorCode.TRANSIENT
    assert events[0].error.retriable is True


async def test_external_client_is_not_closed_by_provider() -> None:
    """注入的 client 由调用方负责关闭——provider 不得擅自关掉它。

    擅自关闭的后果：调用方复用的连接池被静默销毁，
    下一次请求报"client has been closed"，而调用点离这里很远。
    """
    provider, _ = _provider()
    await _collect(provider)
    await provider.aclose()  # 注入的 client，这里应当是 no-op

    # 仍然可用
    events = await _collect(provider)
    assert isinstance(events[-1], StopEvent)


async def test_stream_is_async_iterable_not_coroutine() -> None:
    """``stream()`` 必须返回异步迭代器，**不是** coroutine。

    写成 ``async def`` + ``yield`` 的话调用方拿到 coroutine，
    ``async for`` 会报 ``'coroutine' object is not async iterable``。
    这类错误在类型检查里看不出来（注解都是 ``AsyncIterator``）。
    """
    provider, _ = _provider()
    stream = provider.stream(
        [UserMessage(content="hi", timestamp=ts(1))],
        [],
        model="m",
        signal=_NeverCancelled(),
    )
    assert hasattr(stream, "__aiter__")


# ---------------------------------------------------------------------------
# 流式超时：防「一直有心跳但不出内容」的挂死（2026-09-23 实测缺陷）
# ---------------------------------------------------------------------------


def _stream_handler(chunks: bytes, *, pause_s: float, pause_after: int):
    """造一个**会在中途停住**的流式 handler。

    ``pause_after`` 之前正常吐数据；之后停 ``pause_s`` 秒再吐完剩下的数据。
    两条超时用例共用它：一个验证"停太久被空闲闸砍掉"，
    一个验证"慢慢滴答被总时长闸砍掉"。
    """

    async def _gen():  # type: ignore[no-untyped-def]
        yield chunks
        await asyncio.sleep(pause_s)
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_gen(),
            headers={"content-type": "text/event-stream"},
        )

    return handler


async def _collect_events(provider: OpenAICompatProvider) -> list[Any]:
    return [
        event
        async for event in provider.stream(
            [UserMessage(content="hi", timestamp=ts(1))],
            [],
            model="m",
            signal=_NeverCancelled(),
        )
    ]


async def test_idle_timeout_cuts_a_stalled_stream() -> None:
    """**G87 的靶子**：SSE 停住超过空闲上限 → 必须收到**可见**的超时错误。

    为什么 ``httpx.Timeout`` 挡不住（对应模块常量处的注释）：读超时管的是
    单次读，心跳一响就重置。这里验的是**空闲闸**——停了 0.5 s、空闲上限
    0.1 s，必须被砍掉。

    注入后（空闲闸失效）会发生什么：流在 0.5 s 后正常吐完 DONE →
    **没有 ErrorEvent** → 断言红。不会 hang，是确定性红。
    """
    transport = httpx.MockTransport(_stream_handler(b"", pause_s=0.5, pause_after=0))
    provider = OpenAICompatProvider(
        base_url="https://example.invalid/v1",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
        stream_idle_timeout_s=0.1,
        stream_total_timeout_s=30.0,
    )
    events = await _collect_events(provider)
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1, f"应恰好一条超时错误事件，实际 {errors}"
    assert errors[0].error.code == ErrorCode.TRANSIENT
    assert "空闲超时" in errors[0].error.message


async def test_total_timeout_guards_slow_drip() -> None:
    """**G88 的靶子**：一直"有数据、每次都不算超时"的流被整条时长闸砍掉。

    空闲闸防不住这种：每个 chunk 都在空闲上限内到达，但整条流永远不结束
    （2026-09-23 挂死 1 小时的那些连接正是这种形状）。所以 handler 用的是
    **快速滴答**（每 5 ms 一行），让"单行不超时、整条超时"这个条件成立。

    注入后（总闸失效）流会正常走完 40 行 → 没有 ErrorEvent → 断言红（不 hang）。
    """

    async def _drip():  # type: ignore[no-untyped-def]
        for _ in range(40):
            await asyncio.sleep(0.005)
            yield b'data: {"choices":[]}\n\n'
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_drip(), headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatProvider(
        base_url="https://example.invalid/v1",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        stream_idle_timeout_s=5.0,
        stream_total_timeout_s=0.05,
    )
    events = await _collect_events(provider)
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1, f"应恰好一条整体超时错误事件，实际 {errors}"
    assert "整体超时" in errors[0].error.message


async def test_normal_stream_has_no_timeout_error() -> None:
    """回归：正常（一次吐完）的流不受两个闸影响——不能误伤。"""
    provider, _ = _provider()
    events = await _collect(provider)
    assert not [e for e in events if isinstance(e, ErrorEvent)]
