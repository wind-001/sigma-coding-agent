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

import asyncio
import json
import time
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


#: 一次流式请求的**空闲**上限（秒）：SSE 两行之间超过这个间隔就断开。
#:
#: 为什么 ``httpx.Timeout`` 挡不住挂死
#:     ``httpx.Timeout(120.0)`` 约束的是**单次读**操作——SSE 每收到一个
#:     chunk（含心跳）就重置计时。模型侧只要定期发心跳，120 s 的读超时
#:     **永远不会触发**：实测（2026-09-23 晚间 DeepSeek 高峰）有两个连接
#:     挂死 1 小时零产出，而同一时刻其他连接每轮 4 s 完成。
#:     「读超时」能防"半天没动静"，防不住"一直有心跳但不出内容"。
#:
#: 为什么取 60 而不是更小
#:     厂商心跳间隔常见 15–30 s；正常首 token <5 s、高峰 <30 s。60 s 是
#:     心跳间隔的 2–4 倍——不误伤正常请求，又能在一个心跳周期内没数据时止损。
STREAM_IDLE_TIMEOUT_S: float = 60.0

#: 一次流式请求的**整条时长**上限（秒）：无论如何到点就断。
#:
#: 依据：本次轮数扫描的实测单轮耗时 3–30 s（r20/r30 每轮约 4 s完成
#: 20–25 轮任务），单轮最坏 ~90 s；300 s 是它的 3 倍以上。它防的是
#: "慢速滴答"——每次都来一点数据、永远不结束的那种挂起。
STREAM_TOTAL_TIMEOUT_S: float = 300.0

#: 建连阶段（TCP / DNS / TLS 握手）的请求级上限（秒）。
#:
#: 为什么必须显式设而不能共用一个大标量
#:     ``httpcore`` 的 DNS 解析（``getaddrinfo``）跑在**线程池**里，
#:     不受 httpx 的 connect 超时约束（encode/httpcore 的已知行为）：
#:     解析器卡住时，120 s 的 connect 超时永远等不到触发点。
#:     2026-09-24 syn-012 挂死 8.5 h 零产出正是这个形状。
#:     正常建连（含 TLS）实测 <5 s，15 s 已是 3 倍余量；
#:     解析器卡死则由外层的任务级兜底闸（见下）砍掉。
REQUEST_CONNECT_TIMEOUT_S: float = 15.0

#: **任务级兜底**（秒）：整条请求（建连 → 发送 → 收流 → 结束）的总期限。
#:
#: 为什么是 360 而不是直接沿用总时长闸的 300
#:     内层最坏合法路径 ≈ 总时长闸 300 s + 最后一次取行最多再等
#:     空闲闸 60 s ≈ 360 s。取值**小于**它，内层闸本可自决的超时会被
#:     外层抢报（两个闸的语义互相污染——与"空闲闸不掺总时长"是同一条
#:     纪律）；取值**远大于**它，建连挂死要等太久才止损。
#:     360 = 300 + 60 恰好覆盖内层最坏情况：超过它还没结束的请求，
#:     只可能卡在内层两闸**覆盖不到的阶段**（DNS / TCP / TLS / 发送），
#:     那正是要兜的形状。
TASK_TOTAL_TIMEOUT_S: float = STREAM_TOTAL_TIMEOUT_S + STREAM_IDLE_TIMEOUT_S


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
        stream_idle_timeout_s: 流式两行之间的空闲上限（默认见模块常量）。
            **测试要传小值**——否则"验超时"的用例要真等一分钟。
        stream_total_timeout_s: 一次流式请求的整条时长上限。
        task_total_timeout_s: 整条请求（含建连等读循环之前的阶段）的总期限。
            **必须 ≥ 内层最坏合法路径（总闸 + 空闲闸）**，否则两个闸的
            语义会互相污染。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        provider_name: str = "openai-compat",
        stream_idle_timeout_s: float = STREAM_IDLE_TIMEOUT_S,
        stream_total_timeout_s: float = STREAM_TOTAL_TIMEOUT_S,
        task_total_timeout_s: float = TASK_TOTAL_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()
        self._idle_timeout_s = stream_idle_timeout_s
        self._total_timeout_s = stream_total_timeout_s
        self._task_total_timeout_s = task_total_timeout_s

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

    def _request_timeout(self, timeout_s: float | None) -> httpx.Timeout:
        """构造请求级超时：**四个阶段都必须有界**，重点是 connect。

        为什么 connect 必须显式压小
            ``httpcore`` 的 DNS 解析跑在线程池里，不受 httpx 超时约束——
            标量超时（如 ``Timeout(120.0)``）对卡死的解析器无能为力。
            显式给 connect / pool 设 ``REQUEST_CONNECT_TIMEOUT_S``，
            再由外层任务级兜底闸做最后的保险（两者是纵深防御，不是重复）。

        读超时为什么给总时长闸的值
            单次读的挂死由**空闲闸**负责（语义更准、错误信息更有用），
            httpx 的读超时只需要"存在且有界"，取总时长闸的值保证它
            永远不会抢在空闲闸之前触发。
        """
        if timeout_s is not None:
            # 调用方给了整体上限：connect 取它与建连上限的较小值，
            # 保证任何阶段都不超过调用方的预算。
            return httpx.Timeout(
                timeout_s, connect=min(timeout_s, REQUEST_CONNECT_TIMEOUT_S)
            )
        return httpx.Timeout(
            self._total_timeout_s,
            connect=REQUEST_CONNECT_TIMEOUT_S,
            pool=REQUEST_CONNECT_TIMEOUT_S,
        )

    async def _aiter_stream_raw(
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
        # 注意：**本层不累积 tool_calls**。
        #
        # 2026-09-20 删掉了一份从未被使用的累积（原 `tool_acc`）——
        # 它每次流结束都被 `del` 掉，而真正的拼装由 agent 层用
        # `sigma_ai.tool_calls.ToolCallAssembler` 完成。
        #
        # 协议层只负责把分片翻译成带 `index` 的事件：
        # **"按 index 归属"这个信息在事件里已经带上了**，不需要在这里再存一份。
        # 两份累积意味着两处可错的代码，而多工具 index 交错正是最容易写错的地方。

        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                headers=self._headers(),
                timeout=self._request_timeout(timeout_s),
            ) as response:
                if response.status_code >= 400:
                    raw_text = (await response.aread()).decode(
                        "utf-8", errors="replace"
                    )
                    payload = _error_from_response(response.status_code, raw_text)
                    yield ErrorEvent(error=payload)
                    return

                # 读循环改成**显式 anext + 两个闸**（不能只靠 httpx 的读超时）：
                # 挂死时 ``async for`` 会一直 await 在 anext 上，循环体里的
                # 时间检查**根本执行不到**——所以超时必须套在 await 外面。
                started_at = time.monotonic()
                line_iter = response.aiter_lines()
                while True:
                    elapsed_s = time.monotonic() - started_at
                    if elapsed_s > self._total_timeout_s:
                        yield ErrorEvent(
                            error=ProviderErrorPayload(
                                code=ErrorCode.TRANSIENT,
                                message=(
                                    f"流式响应整体超时（>{self._total_timeout_s:.0f}s "
                                    "未结束）"
                                ),
                            )
                        )
                        return
                    try:
                        # **只传空闲上限**（不传"剩余总时长"）：若把两者取 min，
                        # 总时长将尽时 wait_for 的超时会趋近 0，于是"总时长耗尽"
                        # 被误报成"空闲超时"——两个闸的语义必须互不污染。
                        line = await asyncio.wait_for(
                            line_iter.__anext__(),
                            timeout=self._idle_timeout_s,
                        )
                    except StopAsyncIteration:
                        break
                    except (asyncio.TimeoutError, TimeoutError):
                        # 丢弃必须可见：挂起不是"没发生"，是一条被砍掉的响应。
                        yield ErrorEvent(
                            error=ProviderErrorPayload(
                                code=ErrorCode.TRANSIENT,
                                message=(
                                    f"流式响应空闲超时（>{self._idle_timeout_s:.0f}s "
                                    "没有新数据）"
                                ),
                            )
                        )
                        return

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
                            function = call.get("function") or {}
                            yield ToolCallDelta(
                                index=int(call.get("index", 0)),
                                id=call.get("id"),
                                name=function.get("name"),
                                arguments_delta=function.get("arguments") or "",
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
        """外层：**任务级兜底闸**（2026-09-24 syn-012 事故的修复）。

        为什么必须有这一层
            内层的两个闸（空闲 60 s / 总时长 300 s）都活在**读循环里**，
            覆盖不到读循环之前的阶段：DNS 解析、TCP/TLS 建连、请求发出、
            响应头到达。这些阶段挂死时，第一个事件永远不会产出——
            syn-012 挂 8.5 h 零产出正是这个形状（同一时刻其他档位正常）。
            而且httpcore 的 DNS 解析跑在线程池里，连请求级 connect
            超时都约束不到它，所以还需要这一层任务级保险。

        机制
            不是"把整个生成器包进一个 ``wait_for``"（那样事件只能最后
            一次性吐出，流式失去意义），而是给整条请求立一个 **deadline**，
            每次等下一个事件时用**剩余预算**做 wait_for。效果等价于
            整条请求被 wait_for 包住，同时保留增量产出。

        为什么 wait_for 传"剩余预算"而不是固定值
            传固定值 = 每个事件各有一份完整预算，无限慢滴永远砍不掉
            （``test_task_level_deadline_covers_whole_request`` 钉死这一点）。
            传剩余预算才是"整条请求的总期限"。

        取消语义
            ``wait_for`` 不会吞掉 inner 主动抛出的 ``CancelledError``
            （取消令牌的语义原样穿透），只把**超时**归一化成
            ``TRANSIENT`` 错误事件——遵循本项目"流式错误即事件、
            不裸抛异常打断流"的既有约定。
        """
        inner = self._aiter_stream_raw(
            messages,
            tools,
            model=model,
            signal=signal,
            sampling=sampling,
            options=options,
            timeout_s=timeout_s,
        )
        deadline = time.monotonic() + self._task_total_timeout_s
        while True:
            try:
                event = await asyncio.wait_for(
                    inner.__anext__(),
                    timeout=deadline - time.monotonic(),
                )
            except StopAsyncIteration:
                return
            except (asyncio.TimeoutError, TimeoutError):
                yield ErrorEvent(
                    error=ProviderErrorPayload(
                        code=ErrorCode.TRANSIENT,
                        message=(
                            f"任务级超时（整个请求 >{self._task_total_timeout_s:.0f}s "
                            "未完成，可能卡在连接建立等读循环之前的阶段）"
                        ),
                    )
                )
                return
            yield event

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


