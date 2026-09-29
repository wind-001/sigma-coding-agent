"""流式事件：五类判别联合。

为什么必须是一个统一契约
    UI、测试**消费同一个类型**。不要在 UI 层另造一套事件——
    那会导致「测试通过但 UI 显示错」这类只在集成时才暴露的问题。

载体判据（P6 批次C，三问判定——AGENTS.md 第 3 条）
    六类事件是 **frozen dataclass**，不是 BaseModel。三问皆否：
    ① 不生成 JSON schema（不是工具参数）；② 生产路径**不序列化**——
    事件被 loop 的 isinstance 分派后立即聚合成 ``AssistantMessage``，
    真正落盘的是后者（JSONL），事件本身是瞬时通知；
    ③ SSE 分帧的字段转换由 ``openai/protocol.py`` 手工完成，
    构造器不需要再校验。与 ``sigma.events.lifecycle`` 的
    "事件是瞬时通知，不是领域模型" 同一条判据。

``kw_only`` 为什么开
    六类事件全部按关键字构造（provider / fake / 测试三处实证）；
    锁死 kw_only 后，"``type`` 判别字段带默认值排在必填字段前"的
    声明顺序得以保留，且位置传参的误用被构造器直接拒绝。

为什么工具调用是"增量"
    provider 侧的工具调用参数是**分片到达**的（``arguments_delta`` 是 JSON
    片段字符串，可能在任何字节处被切断）。
    累积逻辑**不在本层**——它在 ``sigma.agent`` 的 loop 里（批次 4），
    因为「什么时候认为参数收完了」是循环的职责，不是协议的职责。

``text_signature`` 为什么出现在增量事件里
    签名是 provider 在流末尾才给出的。逐块透传它，让上层能在拼装完整文本时
    一并拿到签名——否则又要在上层的缓存里维护一份，容易丢。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sigma.providers.errors import ProviderErrorPayload
from sigma.providers.messages import StopReason, Usage


@dataclass(frozen=True, kw_only=True)
class TextDelta:
    """文本增量。"""

    type: Literal["text_delta"] = "text_delta"
    text: str
    text_signature: str | None = None


@dataclass(frozen=True, kw_only=True)
class ThinkingDelta:
    """思考增量。

    P1 的 OpenAI 兼容 provider 不会产生它（OpenAI 系不走 thinking 块），
    但类型必须先有——否则将来加 Anthropic 时要回头改事件契约，
    而事件契约是 UI / 测试两方共用的，改动面很大。
    """

    type: Literal["thinking_delta"] = "thinking_delta"
    thinking: str
    thinking_signature: str | None = None


@dataclass(frozen=True, kw_only=True)
class ToolCallDelta:
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


@dataclass(frozen=True, kw_only=True)
class UsageEvent:
    """用量事件。

    注意：OpenAI 兼容协议**默认不返回它**，需要显式请求
    （``StreamOptions.include_usage``）。缺了它，本层的用量只能靠估算。
    """

    type: Literal["usage"] = "usage"
    usage: Usage


@dataclass(frozen=True, kw_only=True)
class StopEvent:
    """结束事件。流正常终止时吐一个。"""

    type: Literal["stop"] = "stop"
    stop_reason: StopReason


@dataclass(frozen=True, kw_only=True)
class ErrorEvent:
    """错误事件。

    **错误是事件而不是异常**：流中途出错时，上层需要拿到"已经产出的部分"
    与"错误本身"两样东西。抛异常会丢掉前者。

    这里放的是 :class:`~sigma.providers.errors.ProviderErrorPayload`（纯数据）
    而**不是** ``ProviderError``（异常）——事件要能全字段相等比较，
    异常实例没有值语义（两个同码异常互不相等）。需要抛的时候用
    ``ProviderError.from_payload(event.error)`` 还原。
    """

    type: Literal["error"] = "error"
    error: ProviderErrorPayload


StreamEvent = (
    TextDelta | ThinkingDelta | ToolCallDelta | UsageEvent | StopEvent | ErrorEvent
)
"""五类（含 ThinkingDelta 共六类）流式事件的判别联合。"""
