"""Anthropic Messages 协议的 Provider 实现（``BaseProvider`` 第二实现）。

**为什么直连 httpx 而不用 anthropic 官方 SDK**（与 ``openai/provider.py``
同一条结论，写在这里是因为这个问题每个协议实现都会被再问一遍）

1. **零新增依赖**：``httpx`` 早已在 ``pyproject.toml`` 的 ``dependencies`` 里。
2. **SDK 不是少写代码，是多一层翻译**：它只代劳 HTTP 与 SSE 分帧，
   ``流事件 → StreamEvent`` 仍要自己写。
3. **SDK 恰好藏起了本层要验证的协议细节**（``event:`` 行的分帧、
   ``message_stop`` 结束锚、五个事件的接续关系），而那正是 G-ANTH-2/6
   要钉住的东西。
4. **抽象层级对齐**：``FakeProvider`` 在事件层，本类在协议层；
   两个协议实现（openai / anthropic）互相**不依赖**——所以两个
   provider 之间的同构代码（两闸 + 任务级兜底）是**镜像复制**而不是
   抽公共基类，代价由两边各自的测试钉住行为一致。

v1 显式不做（详规 §1 范围红线，写清边界避免被读成"忘了"）
    prompt caching 控制参数、vision 的主动发送、extended thinking 的
    开启参数、batches、prompt 前缀缓存开关。

``max_tokens`` 为什么有缺省值 4096
    Anthropic 协议里 ``max_tokens`` 是**必填**且没有服务端默认值——
    不发这个字段直接 400。缺省 4096 是**协议要求的兜底**，不是模型默认
    （上层给了 ``SamplingParams.max_tokens`` 就用它）。

断流检测（G-ANTH-6）
    Anthropic 的正常结束锚是 ``message_stop`` 事件。流在见到它之前 EOF，
    是**被截断的响应**——静默按 stop 收尾会让半截 tool_call 参数在上层
    拼出非法 JSON，症状（模型参数错误）完全不指向根因（断流）。
    口径与 openai 侧的 ``saw_finish`` / ``saw_done`` 双锚对齐：
    这里单锚 ``message_stop``（Anthropic 没有第二个终止信号）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

from sigma.providers.anthropic.convert import messages_to_anthropic, tools_to_anthropic
from sigma.providers.anthropic.protocol import (
    _error_from_response,
    _error_from_stream_event,
    _map_stop_reason,
    _parse_usage,
)
from sigma.providers.anthropic.sse import SseEventParser
from sigma.providers.base import BaseProvider
from sigma.providers.errors import ErrorCode, ProviderErrorPayload
from sigma.providers.events import (
    ErrorEvent,
    StopEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageEvent,
)
from sigma.providers.messages import Usage
from sigma.providers.tokens import estimate_messages

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sigma.providers.base import CancelToken, SamplingParams, StreamOptions
    from sigma.providers.messages import LlmMessage


#: ``anthropic-version`` 请求头的值。API 版本与 SDK 大版本无关，
#: 是服务端固定的一个日期串；2023-06-01 是 Messages API 的现行稳定版。
ANTHROPIC_VERSION: str = "2023-06-01"

#: ``max_tokens`` 的协议兜底值。见模块 docstring——**这是协议必填字段的
#: 缺省，不是模型默认**；上层给了 ``SamplingParams.max_tokens`` 就用它。
DEFAULT_MAX_TOKENS: int = 4096

# ---------------------------------------------------------------------------
# 流式三闸 + 任务级兜底：与 openai/provider.py 的常量**逐值镜像**。
#
# 为什么复制而不是共享一个常量模块：两个协议实现互相不依赖（见模块
# docstring 第 4 条），共享常量会在它们之间造出第一条横向依赖——
# 有了第一条，"顺手"引用对面的解析函数就是下一步。数值与语义必须
# 保持一致这一点，由两边各自的超时测试钉住。
# ---------------------------------------------------------------------------

#: 一次流式请求的**空闲**上限（秒）：SSE 两行之间超过这个间隔就断开。
#: 取值依据（厂商心跳 15–30 s，正常首 token <30 s）见 openai/provider.py 同名常量。
STREAM_IDLE_TIMEOUT_S: float = 60.0

#: 一次流式请求的**整条时长**上限（秒）：防"慢速滴答"式挂起。
STREAM_TOTAL_TIMEOUT_S: float = 300.0

#: 建连阶段（TCP / DNS / TLS 握手）的请求级上限（秒）。
#: 为什么必须显式设（httpcore 的 DNS 跑在线程池、不受读超时约束），
#: 见 openai/provider.py 同名常量处的 syn-012 事故记录。
REQUEST_CONNECT_TIMEOUT_S: float = 15.0

#: **任务级兜底**（秒）：整条请求的总期限 = 总时长闸 + 空闲闸，
#: 恰好覆盖内层最坏合法路径；超过它还没结束的请求只可能卡在
#: 内层两闸覆盖不到的阶段（DNS / TCP / TLS / 发送）。
TASK_TOTAL_TIMEOUT_S: float = STREAM_TOTAL_TIMEOUT_S + STREAM_IDLE_TIMEOUT_S


class UnmappedAnthropicEvent(UserWarning):
    """流里出现未收录的事件类型（已跳过，流继续）。"""


class UnmappedAnthropicDelta(UserWarning):
    """``content_block_delta`` 出现未收录的子类型（已跳过，流继续）。"""


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API 的 provider。

    用法::

        provider = AnthropicProvider(
            base_url="https://api.anthropic.com", api_key="sk-ant-..."
        )
        async for event in provider.stream(messages, tools, model="claude-sonnet-4-5",
                                           signal=token):
            ...

    参数
        base_url: 不带 ``/v1/messages`` 后缀，例如 ``https://api.anthropic.com``。
        api_key: 走 ``x-api-key`` 头（**不是** OpenAI 的 ``Authorization: Bearer``）。
        client: 注入的 httpx client。**测试用 ``MockTransport`` 从这里进**，
            不需要真实网络。
        stream_idle_timeout_s / stream_total_timeout_s / task_total_timeout_s:
            三道闸的秒数，语义与 openai 侧完全一致；**测试要传小值**。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        provider_name: str = "anthropic",
        stream_idle_timeout_s: float = STREAM_IDLE_TIMEOUT_S,
        stream_total_timeout_s: float = STREAM_TOTAL_TIMEOUT_S,
        task_total_timeout_s: float = TASK_TOTAL_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._owns_client = client is None
        if client is None:
            import httpx  # noqa: PLC0415 — 懒加载:注入 client 的测试/回放路径不付导入钱

            client = httpx.AsyncClient()
        self._client = client
        self._idle_timeout_s = stream_idle_timeout_s
        self._total_timeout_s = stream_total_timeout_s
        self._task_total_timeout_s = task_total_timeout_s

    async def aclose(self) -> None:
        """关闭自建的 client。外部注入的 client 由调用方负责。"""
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        """Anthropic 的鉴权是 ``x-api-key`` + ``anthropic-version`` 双头。

        与 OpenAI 的 ``Authorization: Bearer`` 完全不同——照抄 openai 侧的
        头会得到一个难以理解的 401。
        """
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
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
        """拼请求体。消息与工具的形状转换全部委托 convert 模块。"""
        conversion = messages_to_anthropic(messages)

        body: dict[str, Any] = {
            "model": model,
            # 协议必填：Anthropic 无服务端默认值，缺了直接 400（见模块 docstring）。
            "max_tokens": (
                sampling.max_tokens
                if sampling is not None and sampling.max_tokens is not None
                else DEFAULT_MAX_TOKENS
            ),
            "stream": True,
        }
        if conversion.system is not None:
            body["system"] = conversion.system
        body["messages"] = conversion.messages
        if tools:
            body["tools"] = tools_to_anthropic(tools)

        if sampling is not None:
            # 只传非 None 的字段——传 null 可能被服务端当成非法值。
            for name in ("temperature", "top_p"):
                value = getattr(sampling, name)
                if value is not None:
                    body[name] = value

        # ``options`` 的两个字段在 Anthropic 侧都没有对应物：
        # usage 恒随流返回（message_start / message_delta），无需 include_usage；
        # response_format 协议不存在。接受参数但显式忽略——签名由
        # BaseProvider 钉死，两个实现必须同形。
        _ = options

        return body

    def _request_timeout(self, timeout_s: float | None) -> httpx.Timeout:
        """请求级超时：四阶段都有界，connect 显式压小（理由见 openai 同名方法）。"""
        import httpx  # noqa: PLC0415 — 懒加载(见 __init__)

        if timeout_s is not None:
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
        """真正干活的地方：发请求、按 SSE 分帧、把五类事件翻译成 StreamEvent。

        **本层不累积工具参数**（与 openai 侧同一立场）：只把分片翻译成带
        ``index`` 的事件，拼装归上层 ``ToolCallAssembler``。唯一例外是
        "tool_use 块收到了 start 却一个 delta 都没有"——这时补发一个空分片，
        否则这个调用会在上层无声消失（见 ``content_block_stop`` 分支）。
        """
        import httpx  # noqa: PLC0415 — 懒加载:注入 MockTransport 的测试/回放路径不付导入钱

        body = self._build_body(
            messages, tools, model=model, sampling=sampling, options=options
        )
        url = f"{self._base_url}/v1/messages"

        # 跨事件状态：
        #   输入侧 usage 只在 message_start、输出侧只在 message_delta——
        #   UsageEvent 要等两份都到齐（或缺席按 0）才能发。
        prompt_tokens = 0
        cached_tokens = 0
        stop_reason: str = "stop"
        saw_message_stop = False
        # index → (id, name)：content_block_start 里声明的 tool_use 块。
        # **这不是参数累积**——只记身份，参数分片原样透传。
        tool_blocks: dict[int, tuple[str, str]] = {}
        # 已为哪个 index 吐过 ToolCallDelta（id/name 只随第一个分片走，
        # 与 openai 侧"第二个分片不带 id/name"的口径一致）。
        emitted_tool_index: set[int] = set()

        parser = SseEventParser()

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

                # 显式 anext + 两闸，结构与 openai 侧逐行镜像（注释只留差异点）。
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
                        line = await asyncio.wait_for(
                            line_iter.__anext__(),
                            timeout=self._idle_timeout_s,
                        )
                    except StopAsyncIteration:
                        break
                    except (asyncio.TimeoutError, TimeoutError):
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
                        sse_event = parser.feed(line)
                    except json.JSONDecodeError:
                        # 半截 JSON 不静默跳过也不中断流——记错误事件让上层可见
                        # （与 openai 侧同一折中）。
                        yield ErrorEvent(
                            error=ProviderErrorPayload(
                                code=ErrorCode.INVALID_REQUEST,
                                message=f"SSE 行不是合法 JSON：{line[:200]}",
                            )
                        )
                        continue

                    if sse_event is None:
                        continue

                    payload = sse_event.data
                    if not isinstance(payload, dict):
                        # 合法 JSON 但不是对象：与坏 JSON 同级处置——可见、记事件、
                        # 不中断流（openai 侧 test_non_dict_sse_data 同款）。
                        yield ErrorEvent(
                            error=ProviderErrorPayload(
                                code=ErrorCode.INVALID_REQUEST,
                                message=f"SSE 事件不是 JSON 对象：{line[:200]}",
                            )
                        )
                        continue

                    event_type = sse_event.event

                    if event_type == "message_start":
                        # 输入侧 usage 在这里；只记录不发事件（详规 §4 的映射表）。
                        message = payload.get("message") or {}
                        if isinstance(message, dict):
                            start_usage = message.get("usage") or {}
                            if isinstance(start_usage, dict):
                                start = _parse_usage(start_usage)
                                prompt_tokens = start.prompt_tokens
                                cached_tokens = start.cached_tokens

                    elif event_type == "content_block_start":
                        block = payload.get("content_block") or {}
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            index = int(payload.get("index") or 0)
                            tool_blocks[index] = (
                                str(block.get("id") or ""),
                                str(block.get("name") or ""),
                            )
                        # text / thinking 块的 start 无信息量，忽略。

                    elif event_type == "content_block_delta":
                        index = int(payload.get("index") or 0)
                        delta = payload.get("delta") or {}
                        delta_type = (
                            delta.get("type") if isinstance(delta, dict) else None
                        )
                        if delta_type == "text_delta":
                            yield TextDelta(
                                text=str((delta.get("text") or "")),
                                text_signature=None,  # Anthropic 的 text 块没有签名位
                            )
                        elif delta_type == "input_json_delta":
                            call_id, call_name = tool_blocks.get(index, ("", ""))
                            if tool_blocks.pop(index, None) is not None:
                                emitted_tool_index.add(index)
                            yield ToolCallDelta(
                                index=index,
                                id=call_id or None,
                                name=call_name or None,
                                arguments_delta=str(delta.get("partial_json") or ""),
                            )
                        elif delta_type == "thinking_delta":
                            yield ThinkingDelta(thinking=str(delta.get("thinking") or ""))
                        else:
                            # 未知 delta 子类型（如 signature_delta）：丢弃必须可见——
                            # 告警不中断流（与 _map_stop_reason 的未收录告警同源）。
                            import warnings  # noqa: PLC0415 — 告警按需引入

                            warnings.warn(
                                f"未收录的 content_block_delta 子类型：{delta_type!r}，已跳过",
                                UnmappedAnthropicDelta,
                                stacklevel=3,
                            )

                    elif event_type == "content_block_stop":
                        # 零分片的 tool_use 块补一个空分片：start 见过、delta 一个
                        # 没来的调用，若不补会在上层无声消失（空参数是合法边界）。
                        index = int(payload.get("index") or 0)
                        if index in tool_blocks and index not in emitted_tool_index:
                            call_id, call_name = tool_blocks[index]
                            emitted_tool_index.add(index)
                            yield ToolCallDelta(
                                index=index,
                                id=call_id or None,
                                name=call_name or None,
                                arguments_delta="",
                            )

                    elif event_type == "message_delta":
                        # 停止原因 + 输出侧 usage 在这里。UsageEvent 只在
                        # message_delta **真的带了 usage** 时发——不带就发零值
                        # 会把上层已有的真实用量覆盖成 0（loop 取最后一个
                        # UsageEvent），比缺数据更糟。缺 usage 不炸（G-ANTH-3）。
                        delta = payload.get("delta") or {}
                        if isinstance(delta, dict) and delta.get("stop_reason"):
                            stop_reason = _map_stop_reason(str(delta["stop_reason"]))
                        delta_usage = payload.get("usage")
                        if isinstance(delta_usage, dict):
                            output = _parse_usage(delta_usage)
                            yield UsageEvent(
                                usage=Usage(
                                    prompt_tokens=prompt_tokens,
                                    completion_tokens=output.completion_tokens,
                                    cached_tokens=cached_tokens,
                                )
                            )

                    elif event_type == "message_stop":
                        # 正常结束的判定锚（G-ANTH-6）。不发事件，只记锚。
                        saw_message_stop = True

                    elif event_type == "error":
                        # 流中途的 error 事件：Anthropic 发完即断流。
                        # 错误即事件（不裸抛），且不再补发 StopEvent。
                        yield ErrorEvent(error=_error_from_stream_event(payload))
                        return

                    elif event_type == "ping":
                        # 保活心跳，忽略。
                        continue

                    else:
                        # 未知事件类型：告警可见但不中断流——新版本协议加事件
                        # 时，旧客户端该"降级继续跑"，而不是崩。
                        import warnings  # noqa: PLC0415 — 告警按需引入

                        warnings.warn(
                            f"未收录的 Anthropic 流事件类型：{event_type!r}，已跳过。"
                            "若是新版协议事件，请补进 _aiter_stream_raw 的分派表",
                            UnmappedAnthropicEvent,
                            stacklevel=3,
                        )

        except httpx.HTTPError as exc:
            # 网络层异常也要变成事件，不能冒泡打断流（上层要拿到已产出部分）。
            yield ErrorEvent(
                error=ProviderErrorPayload(
                    code=ErrorCode.TRANSIENT,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return

        if not saw_message_stop:
            # EOF 之前没见到 message_stop——流被截断了。丢弃必须可见。
            yield ErrorEvent(
                error=ProviderErrorPayload(
                    code=ErrorCode.TRANSIENT,
                    message="流在未收到 message_stop 前结束——响应被截断",
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
        """外层：任务级兜底闸（deadline + 剩余预算 wait_for）。

        为什么需要这一层、wait_for 为什么传"剩余预算"、取消语义如何穿透——
        完整论证见 ``openai/provider.py`` 的同名方法（syn-012 事故的修复），
        本方法与其逐行镜像，只换内层生成器。
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
        """返回异步迭代器（不是 coroutine）——理由见 openai 侧同名方法。"""
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


