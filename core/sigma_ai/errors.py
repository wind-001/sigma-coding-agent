"""错误归一化：把各家 provider 的错误码映射到统一语义。

为什么要归一化
    不同厂商的错误格式、错误码、HTTP 状态码都不一样。上层（loop、压缩、
    重试）需要的是**语义**而不是原始格式——它要判断的是
    "该重试吗""该压缩吗"，不是"这是 429 还是 503"。

**两个类型，不是一个（2026-09-20 修正）**
    原设计只有 ``ProviderError(Exception)``，``ErrorEvent`` 直接持有它。
    实施时发现**它无法序列化**——G3 要求五类事件都能 round-trip，
    而 Pydantic 无法为"持有异常实例的模型"生成 schema。

    修法**不是**开 ``arbitrary_types_allowed``（那只是让报错消失，
    序列化仍会丢 ``code`` / ``raw``，且反序列化后拿到一个空异常）。
    正确做法是**把"数据"与"可抛出"分开**：

    - :class:`ProviderErrorPayload` —— 纯数据（BaseModel），可序列化、可落盘
    - :class:`ProviderError` —— 异常，持有 payload，供 ``raise`` 用

    这样 ``ErrorEvent`` 里放 payload，落盘与回放都不丢信息；
    需要抛的时候用 ``ProviderError.from_payload()`` 还原。

``retriable`` 为什么是派生属性而不是字段
    手填就会填错，而「该重试的没重试」和「不该重试的狂重试」都是线上事故。
    由 ``code`` 推导，只有一处定义。

``CONTEXT_OVERFLOW`` 为什么重要
    它是压缩机制的**两条触发路径之一**（另一条是本地估算超阈值）。
    识别逻辑属于 provider 层，不属于压缩层——所以 P1 就要做对，
    哪怕 P1 还没有压缩（门槛 G6）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    """统一错误码。五类，按"上层该做什么"划分。"""

    RATE_LIMIT = "rate_limit"
    """限流。→ 退避重试"""

    CONTEXT_OVERFLOW = "context_overflow"
    """上下文超限。→ 触发压缩。识别它比处理它重要。"""

    AUTH = "auth"
    """鉴权失败（key 无效 / 权限不足）。→ 立即失败，重试无意义"""

    TRANSIENT = "transient"
    """瞬时故障（网络抖动 / 5xx）。→ 重试"""

    INVALID_REQUEST = "invalid_request"
    """请求本身非法（参数错误 / 模型不存在）。→ 立即失败"""


# 哪些错误码可重试。**唯一的事实来源**，不要在其他地方重复判断。
_RETRIABLE: frozenset[ErrorCode] = frozenset(
    {ErrorCode.RATE_LIMIT, ErrorCode.TRANSIENT}
)


class ProviderErrorPayload(BaseModel):
    """错误的**纯数据**形式。可序列化、可落盘、可回放。

    与 :class:`ProviderError` 分开，是因为事件流要能被记录与重放——
    异常实例做不到这一点。

    ``raw`` 保留原始响应体，便于事后排查厂商差异——
    这是用自研适配层相对 SDK 的一个实际收益。
    """

    code: ErrorCode
    message: str
    raw: dict[str, Any] = Field(default_factory=dict)
    status_code: int | None = None

    @property
    def retriable(self) -> bool:
        """是否可重试。由 ``code`` 派生，不由调用方决定。"""
        return self.code in _RETRIABLE

    def __str__(self) -> str:
        base = f"[{self.code}] {self.message}"
        if self.status_code is not None:
            base += f" (http {self.status_code})"
        return base


class ProviderError(Exception):
    """可抛出的 provider 错误。内部持有一个 :class:`ProviderErrorPayload`。

    ``str(exc)`` 与 ``exc.payload`` 的信息完全一致——
    数据只有一份，异常只是它的可抛出外壳。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        raw: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.payload = ProviderErrorPayload(
            code=code,
            message=message,
            raw=raw if raw is not None else {},
            status_code=status_code,
        )

    @classmethod
    def from_payload(cls, payload: ProviderErrorPayload) -> ProviderError:
        """从数据还原成可抛出的异常。回放场景用。"""
        return cls(
            payload.code,
            payload.message,
            raw=payload.raw,
            status_code=payload.status_code,
        )

    @property
    def code(self) -> ErrorCode:
        return self.payload.code

    @property
    def message(self) -> str:
        return self.payload.message

    @property
    def raw(self) -> dict[str, Any]:
        return self.payload.raw

    @property
    def status_code(self) -> int | None:
        return self.payload.status_code

    @property
    def retriable(self) -> bool:
        """是否可重试。转发到 payload，保持单一事实来源。"""
        return self.payload.retriable

    def __str__(self) -> str:
        return str(self.payload)


def classify_http_status(status_code: int, message: str = "") -> ErrorCode:
    """把 HTTP 状态码映射到错误码。

    这是**兜底**路径：优先走厂商特定的映射函数（如
    ``openai_compat.classify_response``），认不出时用这个。

    ``400`` 需结合 ``message`` 判断是否上下文超限——很多厂商用它表示
    "prompt too long"，而 OpenAI 兼容协议的正式超限码是 ``400`` +
    特定 message 片段。**这是自研适配层必须处理的兼容性差异之一。**
    """
    if status_code == 429:
        return ErrorCode.RATE_LIMIT
    if status_code in (401, 403):
        return ErrorCode.AUTH
    if status_code in (500, 502, 503, 504):
        return ErrorCode.TRANSIENT
    if status_code == 400:
        lowered = message.lower()
        overflow_markers = (
            "context length",
            "context_length",
            "too long",
            "maximum context",
            "reduce the length",
            "prompt is too long",
        )
        if any(marker in lowered for marker in overflow_markers):
            return ErrorCode.CONTEXT_OVERFLOW
        return ErrorCode.INVALID_REQUEST
    if status_code == 404:
        return ErrorCode.INVALID_REQUEST
    return ErrorCode.INVALID_REQUEST
