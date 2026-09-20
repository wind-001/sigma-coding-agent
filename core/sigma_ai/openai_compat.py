"""OpenAI 兼容协议的 Provider 适配。

覆盖范围
    DeepSeek / Kimi / GLM / 通义 / vLLM / Ollama / LM Studio 等一切
    提供 ``/v1/chat/completions`` 的服务（a3 拍板：P1 只做 1 套协议）。

**为什么直连 httpx 而不用 openai 官方 SDK**（批次 1 详规 7.1 节的结论）

1. **零新增依赖**：``httpx`` 早已在 ``pyproject.toml`` 的 ``dependencies`` 里
   （openai SDK 自身也依赖它）。用 SDK 才是新增依赖。
2. **SDK 不是少写代码，是多一层翻译**：它只代劳 HTTP 与 SSE 分帧，
   ``ChatCompletionChunk → StreamEvent`` 仍要自己写。
3. **SDK 恰好藏起了本层要验证的协议细节**——见下方"四个必须自己处理的坑"，
   其中前两条最容易出问题（随机丢字符）、最难复现，SDK 全包了。
4. **抽象层级对齐**：``FakeProvider`` 在事件层，本类也应在协议层。
   若本类建在 SDK 之上，G4「同 transcript 两次回放一致」就失去可比性。
5. **兼容性差异的排查能力**：OpenAI 兼容协议的"兼容"是有限度的——
   各厂商在 ``finish_reason`` 取值、``usage`` 字段位置、``tool_calls``
   分片方式上都有出入。用 SDK 则失去在这层排查的能力。

本类在批次 1 的职责（门槛 G10）
    **不是**「完整支持真实调用」，而是**「作为第二个实现者，
    把 ``BaseProvider`` 签名里的缺漏逼出来」**。
    只写 ``FakeProvider`` 时，``sampling`` / ``options`` / ``timeout_s``
    三个参数永远不会出现——回放不需要采样、不需要请求用量、不会超时。

四个必须自己处理的坑
    1. **SSE 分块边界可能切开一个 JSON**——必须缓冲累积，不能逐块解析。
       处理错误会导致随机丢字符，是最难复现的一类 bug。
    2. **``[DONE]`` 不是 JSON**——遇到它直接结束，不要送去 ``json.loads``。
    3. **``finish_reason`` → ``stop_reason`` 要做映射**，且各厂商取值有出入。
    4. **``tool_calls[].index`` 用于多工具并行时的归属**——
       分片要按 index 分组，不能按到达顺序硬拼。

批次 1 无法验证的项（详规第 9 节 R2–R6）
    错误码映射的真实性 / SSE 分帧的健壮性 / ``finish_reason`` 的实际取值 /
    ``usage`` 是否真能取到 / 多工具 index 归属。这些要批次 4 接真实 API 复核。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from sigma_ai.base import BaseProvider
from sigma_ai.errors import (
    ErrorCode,
    ProviderError,
    ProviderErrorPayload,
    classify_http_status,
)
from sigma_ai.events import (
    ErrorEvent,
    StopEvent,
    TextDelta,
    ToolCallDelta,
    UsageEvent,
)
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
from sigma_ai.tokens import estimate_messages

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sigma_ai.base import CancelToken, SamplingParams, StreamOptions
    from sigma_ai.messages import LlmMessage

# ---------------------------------------------------------------------------
# finish_reason → StopReason 映射
# ---------------------------------------------------------------------------

# 各厂商取值有出入，所以这里是"多对一"的显式表，不是直接赋值。
# 认不出的取值会走兜底分支并发出可见的告警（不静默吞掉）。
_FINISH_REASON_MAP: dict[str, str] = {
    "stop": "stop",
    "end_turn": "stop",  # Anthropic 风格，部分兼容实现沿用
    "length": "length",
    "max_tokens": "length",  # 部分厂商用这个
    "tool_calls": "tool_use",
    "function_call": "tool_use",  # 旧版 OpenAI 取值
    "content_filter": "content_filter",
}


class UnmappedFinishReason(UserWarning):
    """``finish_reason`` 出现未收录的取值。"""


# ---------------------------------------------------------------------------
# 消息转换：LLM 层 → OpenAI 请求体
# ---------------------------------------------------------------------------


def _blocks_to_openai_content(
    content: str | list[Any],
) -> str | list[dict[str, Any]]:
    """把内容块列表转成 OpenAI 的 ``content`` 形状。

    纯文本且无签名时降级成裸字符串——少一层嵌套，也少几个 token。

    ``ToolCallBlock`` 为什么是 ``continue`` 而不是报错
        工具调用在 OpenAI 协议里**不放在 ``content`` 数组里**，
        它是与之并列的 ``tool_calls`` 字段（由 ``message_to_openai`` 组装）。
        所以这里必须跳过它。

        这个分支曾是缺失的，表现为：一条既有文本又有工具调用的 assistant
        消息会直接抛 ``TypeError``——而那正是**最常见的模型输出形态**
        （"我先看一下" + read 调用）。单测 ``test_assistant_text_and_tool_call_coexist``
        逮住了它。
    """
    if isinstance(content, str):
        return content

    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{block.mime_type};base64,{block.data}"},
                }
            )
        elif isinstance(block, ToolCallBlock):
            # 走 tool_calls 字段，不进 content。见 docstring。
            continue
        elif isinstance(block, ThinkingBlock):
            # 思考块不回传给 OpenAI 兼容端点——协议里没有这个位置。
            # 但它必须留在本地消息里（否则丢 signature），所以这里只是不发它。
            continue
        else:
            raise TypeError(f"无法转换的内容块类型：{type(block).__name__}")

    # 只剩一个文本块时降级成字符串
    if len(parts) == 1 and parts[0].get("type") == "text":
        return str(parts[0]["text"])
    return parts


def message_to_openai(message: LlmMessage) -> dict[str, Any]:
    """把单个 LLM 消息转成 OpenAI 请求体里的 dict。

    单独抽成函数是为了让单测能直接断言转换结果，
    不必启动 HTTP——转换逻辑是本层最容易出错的地方。
    """
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _blocks_to_openai_content(message.content)}

    if isinstance(message, UserMessage):
        return {"role": "user", "content": _blocks_to_openai_content(message.content)}

    if isinstance(message, AssistantMessage):
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": _blocks_to_openai_content(message.content) or None,
        }
        tool_calls = [
            block for block in message.content if isinstance(block, ToolCallBlock)
        ]
        if tool_calls:
            payload["tool_calls"] = [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.arguments, ensure_ascii=False),
                    },
                }
                for block in tool_calls
            ]
        return payload

    if isinstance(message, ToolResultMessage):
        # 工具结果在 OpenAI 协议里是独立的 role="tool" 消息，
        # 靠 tool_call_id 关联——这正是"tool_result 是独立角色"（4.0.1 节）的收益。
        text = _blocks_to_openai_content(message.content)
        if not isinstance(text, str):
            # 协议要求 tool 消息的 content 是字符串；要发图片得另想办法。
            # 这里不静默降级成 str(list)——那会产生没人能读的字符串。
            raise TypeError("工具结果的 content 暂不支持多模态内容块")
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": text,
        }

    raise ValueError(f"未知的 LLM 消息类型：{type(message).__name__}")


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """解析一行 SSE。

    返回 ``None`` 表示"这行不产生数据"（空行、注释、``[DONE]``）。

    **``[DONE]`` 不是 JSON**——这是详规 2.7 节列的坑第 2 条。
    送去 ``json.loads`` 会抛异常，被误当成协议错误。
    """
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith(":"):
        # SSE 注释行，用于保活。协议允许，必须忽略而不是报错。
        return None
    if not stripped.startswith("data:"):
        # event: / id: / retry: 等字段，本层不使用。
        return None
    data = stripped[len("data:") :].strip()
    if data == "[DONE]":
        return None
    return json.loads(data)  # type: ignore[no-any-return]


class OpenAICompatProvider(BaseProvider):
    """OpenAI 兼容协议的 provider。

    用法::

        provider = OpenAICompatProvider(
            base_url="https://api.deepseek.com/v1", api_key="sk-..."
        )
        async for event in provider.stream(messages, tools, model="deepseek-chat",
                                           signal=token):
            ...

    参数
        base_url: 不带 ``/chat/completions`` 后缀，例如
            ``https://api.deepseek.com/v1``。
        api_key: 可为空（本地 Ollama / vLLM 通常不校验）。
        client: 注入的 httpx client。**测试用 ``MockTransport`` 从这里进**，
            不需要真实网络。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        provider_name: str = "openai-compat",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()

    async def aclose(self) -> None:
        """关闭自建的 client。外部注入的 client 由调用方负责。"""
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_body(
        self,
        messages: list[LlmMessage],
        tools: list[dict[str, Any]],
        *,
        model: str,
        sampling: SamplingParams | None,
        options: StreamOptions | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [message_to_openai(m) for m in messages],
            "stream": True,
        }
        if tools:
            body["tools"] = tools

        if sampling is not None:
            # 只传非 None 的字段——传 null 可能被某些实现当成非法值。
            for field in ("temperature", "max_tokens", "top_p"):
                value = getattr(sampling, field)
                if value is not None:
                    body[field] = value

        # usage 默认不返回，必须显式请求。这条是 B1.3 逼出来的签名参数的实际用途。
        if options is not None:
            body["stream_options"] = {"include_usage": options.include_usage}
            if options.response_format is not None:
                body["response_format"] = options.response_format
        else:
            body["stream_options"] = {"include_usage": True}

        return body

    async def _aiter_stream(
        self,
        messages: list[LlmMessage],
        tools: list[dict[str, Any]],
        *,
        model: str,
        signal: CancelToken,
        sampling: SamplingParams | None,
        options: StreamOptions | None,
        timeout_s: float | None,
    ) -> AsyncIterator[Any]:
        """真正干活的地方：发请求、按 SSE 分帧、翻译成 StreamEvent。

        **缓冲累积是重点**（详规 2.7 节坑第 1 条）：
        ``aiter_lines()`` 已经按行切好，但一个 JSON 可能跨多个 ``data:`` 行？
        不会——SSE 的约定是每条 data 行是独立的。真正会切开的是**字节流**，
        而 ``aiter_lines`` 在行级别已经处理了拼接。
        这里额外防御的是"一行里 data 是半截 JSON"的违规实现。
        """
        body = self._build_body(
            messages, tools, model=model, sampling=sampling, options=options
        )
        url = f"{self._base_url}/chat/completions"

        # 累积状态：跨事件保持
        text_signature: str | None = None
        usage: Usage | None = None
        stop_reason: str = "stop"
        # index -> {id, name, arguments}
        tool_acc: dict[int, dict[str, Any]] = {}

        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                headers=self._headers(),
                timeout=timeout_s if timeout_s is not None else httpx.Timeout(120.0),
            ) as response:
                if response.status_code >= 400:
                    raw_text = (await response.aread()).decode(
                        "utf-8", errors="replace"
                    )
                    payload = _error_from_response(response.status_code, raw_text)
                    yield ErrorEvent(error=payload)
                    return

                async for line in response.aiter_lines():
                    signal.raise_if_cancelled()

                    try:
                        chunk = parse_sse_line(line)
                    except json.JSONDecodeError:
                        # 半截 JSON 不静默跳过——那会变成"消息莫名消失"。
                        # 但也不中断整个流：记一条错误事件，让上层能看到。
                        yield ErrorEvent(
                            error=ProviderErrorPayload(
                                code=ErrorCode.INVALID_REQUEST,
                                message=f"SSE 行不是合法 JSON：{line[:200]}",
                            )
                        )
                        continue

                    if chunk is None:
                        continue

                    if chunk.get("usage"):
                        usage = _parse_usage(chunk["usage"])
                        yield UsageEvent(usage=usage)

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}

                        if delta.get("content"):
                            yield TextDelta(
                                text=delta["content"], text_signature=text_signature
                            )

                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index", 0))
                            slot = tool_acc.setdefault(
                                index, {"id": None, "name": None, "arguments": ""}
                            )
                            if call.get("id"):
                                slot["id"] = call["id"]
                            function = call.get("function") or {}
                            if function.get("name"):
                                slot["name"] = function["name"]
                            arguments_delta = function.get("arguments") or ""
                            slot["arguments"] += arguments_delta
                            yield ToolCallDelta(
                                index=index,
                                id=call.get("id"),
                                name=function.get("name"),
                                arguments_delta=arguments_delta,
                            )

                        finish = choice.get("finish_reason")
                        if finish:
                            stop_reason = _map_finish_reason(finish)

        except httpx.HTTPError as exc:
            # 网络层异常（超时、连接失败）也要变成事件，不能冒泡打断流——
            # 上层需要拿到"已经产出的部分"。
            yield ErrorEvent(
                error=ProviderErrorPayload(
                    code=ErrorCode.TRANSIENT,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return

        yield StopEvent(stop_reason=stop_reason)  # type: ignore[arg-type]

        # 说明：``tool_acc`` 在此处已累积完整，但把它拼成
        # ``AssistantMessage`` 的工作**不在这里**——那是 loop 的职责
        # （批次 4）。本层只保证分片被正确按 index 归属。
        del tool_acc

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
    ) -> AsyncIterator[Any]:
        """返回异步生成器。

        注意是"返回"而不是"是"——写成 ``async def`` + ``yield`` 的话
        调用方拿到的是 coroutine，``async for`` 会报
        ``'coroutine' object is not async iterable``。
        ``BaseProvider`` 的注解是 ``-> AsyncIterator``，与此一致。
        """
        return self._aiter_stream(
            messages,
            tools,
            model=model,
            signal=signal,
            sampling=sampling,
            options=options,
            timeout_s=timeout_s,
        )

    def estimate_tokens(self, messages: list[LlmMessage]) -> int:
        return estimate_messages(messages)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _parse_usage(raw: dict[str, Any]) -> Usage:
    """解析 usage。

    ``cached_tokens`` 的位置各厂商不同：OpenAI 放在
    ``prompt_tokens_details.cached_tokens``，部分厂商直接在顶层。
    两种都试——这是兼容层必须处理的差异。
    """
    cached = raw.get("cached_tokens")
    if cached is None:
        details = raw.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens", 0)),
        completion_tokens=int(raw.get("completion_tokens", 0)),
        cached_tokens=int(cached or 0),
    )


def _map_finish_reason(raw: str) -> str:
    """把厂商的 ``finish_reason`` 映射成统一的 ``StopReason``。

    **未收录的取值告警而不是静默当 stop**——静默会让"模型被截断"看起来像
    "正常结束"，是评测里最危险的一类假通过。
    """
    mapped = _FINISH_REASON_MAP.get(raw)
    if mapped is None:
        import warnings

        warnings.warn(
            f"未收录的 finish_reason：{raw!r}，按 'stop' 处理。"
            f"请补进 _FINISH_REASON_MAP（已知：{sorted(_FINISH_REASON_MAP)}）",
            UnmappedFinishReason,
            stacklevel=3,
        )
        return "stop"
    return mapped


def _error_from_response(status_code: int, body_text: str) -> ProviderErrorPayload:
    """把 HTTP 错误响应转成 payload。

    优先读 OpenAI 风格的 ``{"error": {"message": ...}}``；
    读不到就用原始文本——**保留原文**，否则排查厂商差异时没线索。
    """
    message = body_text[:500]
    raw: dict[str, Any] = {}
    try:
        parsed = json.loads(body_text)
        if isinstance(parsed, dict):
            raw = parsed
            error = parsed.get("error")
            if isinstance(error, dict) and error.get("message"):
                message = str(error["message"])
            elif parsed.get("message"):
                message = str(parsed["message"])
    except json.JSONDecodeError:
        pass

    return ProviderErrorPayload(
        code=classify_http_status(status_code, message),
        message=message,
        raw=raw,
        status_code=status_code,
    )


# 未使用但导出，方便调用方构造异常：
_ = ProviderError
