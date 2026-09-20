"""OpenAI 兼容协议的 Provider 实现。

覆盖范围
    DeepSeek / Kimi / GLM / 通义 / vLLM / Ollama / LM Studio 等一切
    提供 ``/v1/chat/completions`` 的服务（a3 拍板：P1 只做 1 套协议）。

**为什么直连 httpx 而不用 openai 官方 SDK**（批次 1 详规 7.1 节的结论）

1. **零新增依赖**：``httpx`` 早已在 ``pyproject.toml`` 的 ``dependencies`` 里
   （openai SDK 自身也依赖它）。用 SDK 才是新增依赖。
2. **SDK 不是少写代码，是多一层翻译**：它只代劳 HTTP 与 SSE 分帧，
   ``ChatCompletionChunk → StreamEvent`` 仍要自己写。
3. **SDK 恰好藏起了本层要验证的协议细节**（SSE 分块边界、``[DONE]``），
   而那两条最容易出问题、最难复现，SDK 全包了。
4. **抽象层级对齐**：``FakeProvider`` 在事件层，本类也应在协议层。
   若本类建在 SDK 之上，G4「同 transcript 两次回放一致」就失去可比性。
5. **兼容性差异的排查能力**：各厂商在 ``finish_reason`` 取值、
   ``usage`` 字段位置、``tool_calls`` 分片方式上都有出入。
   用 SDK 则失去在这层排查的能力。

本类在批次 1 的职责（门槛 G10）
    **不是**「完整支持真实调用」，而是**「作为第二个实现者，
    把 ``BaseProvider`` 签名里的缺漏逼出来」**。
    只写 ``FakeProvider`` 时，``sampling`` / ``options`` / ``timeout_s``
    三个参数永远不会出现——回放不需要采样、不需要请求用量、不会超时。

批次 1 无法验证的项（详规第 9 节 R2–R6）
    错误码映射的真实性 / SSE 分帧的健壮性 / ``finish_reason`` 的实际取值 /
    ``usage`` 是否真能取到 / 多工具 index 归属。
    其中三项已在 2026-09-20 用 `scripts/real_api_smoke.py` 对 DeepSeek
    真实端点复核（见 P1-批次1-详规 §9.1），**R1（取消语义）仍未兑现**。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from sigma_ai.base import BaseProvider
from sigma_ai.errors import ErrorCode, ProviderErrorPayload
from sigma_ai.events import (
    ErrorEvent,
    StopEvent,
    TextDelta,
    ToolCallDelta,
    UsageEvent,
)
from sigma_ai.messages import Usage
from sigma_ai.openai.convert import message_to_openai
from sigma_ai.openai.protocol import (
    _error_from_response,
    _map_finish_reason,
    _parse_usage,
)
from sigma_ai.openai.sse import parse_sse_line
from sigma_ai.tokens import estimate_messages

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sigma_ai.base import CancelToken, SamplingParams, StreamOptions
    # 拆文件时这个导入被漏过一次：**176 个测试全绿**，因为
    # `from __future__ import annotations` 让注解不参与求值，
    # 运行期根本不需要 `LlmMessage`。**只有 mypy 抓住了它。**
    # 教训：搬移代码时，"测试通过"证明不了 import 完整——
    # 类型检查覆盖的是另一面。
    from sigma_ai.messages import LlmMessage


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


