"""流式事件：五类判别联合。

为什么必须是一个统一契约
    UI、落盘、测试**消费同一个类型**。不要在 UI 层另造一套事件——
    那会导致「测试通过但 UI 显示错」这类只在集成时才暴露的问题。

为什么工具调用是"增量"
    provider 侧的工具调用参数是**分片到达**的（``arguments_delta`` 是 JSON
    片段字符串，可能在任何字节处被切断）。
    累积逻辑**不在本层**——它在 ``sigma_agent`` 的 loop 里（批次 4），
    因为「什么时候认为参数收完了」是循环的职责，不是协议的职责。

``text_signature`` 为什么出现在增量事件里
    签名是 provider 在流末尾才给出的。逐块透传它，让上层能在拼装完整文本时
    一并拿到签名——否则又要在上层的缓存里维护一份，容易丢。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from sigma_ai.errors import ProviderErrorPayload
from sigma_ai.messages import StopReason, Usage


class TextDelta(BaseModel):
    """文本增量。"""

    type: Literal["text_delta"] = "text_delta"
    text: str
    text_signature: str | None = None


class ThinkingDelta(BaseModel):
    """思考增量。

    P1 的 OpenAI 兼容 provider 不会产生它（OpenAI 系不走 thinking 块），
    但类型必须先有——否则将来加 Anthropic 时要回头改事件契约，
    而事件契约是 UI / 落盘 / 测试三方共用的，改动面很大。
    """

    type: Literal["thinking_delta"] = "thinking_delta"
    thinking: str
    thinking_signature: str | None = None


class ToolCallDelta(BaseModel):
    """工具调用增量。

    ``arguments_delta`` 是 **JSON 片段字符串**，不是解析后的对象——
    因为分片可能从任意字节处切断，此刻拼出来的 JSON 还不合法。

    ``index`` 用于多工具并行时区分归属：provider 按 index 分组吐分片，
    上层依赖它把分片归到正确的工具上（详见 2.7 节第 5 条）。
    """

    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""
    thought_signature: str | None = None


class UsageEvent(BaseModel):
    """用量事件。

    注意：OpenAI 兼容协议**默认不返回它**，需要显式请求
    （``StreamOptions.include_usage``）。缺了它，本层的用量只能靠估算。
    """

    type: Literal["usage"] = "usage"
    usage: Usage


class StopEvent(BaseModel):
    """结束事件。流正常终止时吐一个。"""

    type: Literal["stop"] = "stop"
    stop_reason: StopReason


class ErrorEvent(BaseModel):
    """错误事件。

    **错误是事件而不是异常**：流中途出错时，上层需要拿到"已经产出的部分"
    与"错误本身"两样东西。抛异常会丢掉前者。

    这里放的是 :class:`~sigma_ai.errors.ProviderErrorPayload`（纯数据）
    而**不是** ``ProviderError``（异常）——异常实例无法被 Pydantic 序列化，
    放进来 G3 的 round-trip 断言就跑不通。需要抛的时候用
    ``ProviderError.from_payload(event.error)`` 还原。
    """

    type: Literal["error"] = "error"
    error: ProviderErrorPayload


StreamEvent = (
    TextDelta | ThinkingDelta | ToolCallDelta | UsageEvent | StopEvent | ErrorEvent
)
"""五类（含 ThinkingDelta 共六类）流式事件的判别联合。"""
