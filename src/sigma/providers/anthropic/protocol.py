"""Anthropic Messages 协议的**协议细节**：stop_reason 映射、usage 解析、错误体归一化。

为什么单独成文件
    与 ``openai/protocol.py`` 同一条判据：这三件事都是**纯函数**——
    入参出参，不碰网络、不持有状态，可以被单测直接打，不必起 HTTP。

    Anthropic 与 OpenAI 的差异全部收在这里（对照 openai/protocol.py 逐项看）：

    - ``stop_reason`` 取值不同（``end_turn`` / ``stop_sequence`` / ``max_tokens`` /
      ``tool_use``），且事件序里它出现在 ``message_delta`` 而非 choice 里；
    - ``usage`` 字段名不同（``input_tokens`` / ``output_tokens``），
      缓存命中叫 ``cache_read_input_tokens``，且**拆在两个事件里**：
      输入侧在 ``message_start``、输出侧在 ``message_delta``——拼接在
      provider 里做，这里只负责单份解析；
    - 错误体是 ``{"type": "error", "error": {"type": ..., "message": ...}}``，
      错误类型是**字符串枚举**而非 HTTP 状态码（529 overloaded 甚至不是
      标准 HTTP 码），所以分类以 ``error.type`` 为权威、状态码为辅。
"""

from __future__ import annotations

import json
from typing import Any

from sigma.providers.errors import ErrorCode, ProviderErrorPayload
from sigma.providers.messages import Usage


# ---------------------------------------------------------------------------
# stop_reason → StopReason 映射（详规 §4：四值，单测逐行钉住）
# ---------------------------------------------------------------------------

# 多对一的显式表。Anthropic 官方就这四个取值，没有第五个——
# 出现第五个只可能是新版本协议或网关私改，告警可见（不静默当 stop）。
_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_use",
    "max_tokens": "length",
}


class UnmappedStopReason(UserWarning):
    """``stop_reason`` 出现未收录的取值。"""


def _map_stop_reason(raw: str) -> str:
    """把 Anthropic 的 ``stop_reason`` 映射成统一的 ``StopReason``。

    **未收录的取值告警而不是静默当 stop**——静默会让"模型被截断"看起来像
    "正常结束"，是评测里最危险的一类假通过。与 openai 侧同一条纪律。
    """
    mapped = _STOP_REASON_MAP.get(raw)
    if mapped is None:
        import warnings

        warnings.warn(
            f"未收录的 stop_reason：{raw!r}，按 'stop' 处理。"
            f"请补进 _STOP_REASON_MAP（已知：{sorted(_STOP_REASON_MAP)}）",
            UnmappedStopReason,
            stacklevel=3,
        )
        return "stop"
    return mapped


# ---------------------------------------------------------------------------
# usage 解析
# ---------------------------------------------------------------------------


def _parse_usage(raw: dict[str, Any]) -> Usage:
    """解析单份 usage（``message_start`` 或 ``message_delta`` 里的那份）。

    字段名与 OpenAI 不同：``input_tokens`` / ``output_tokens`` /
    ``cache_read_input_tokens``（缓存**读**命中——D4 的核心指标）。
    ``or 0`` 不是摆设：显式 ``null`` 时 ``.get(key, 0)`` 拿到的是 None，
    ``int(None)`` 直接抛 TypeError——与 openai 侧同一条防线。
    """
    return Usage(
        prompt_tokens=int(raw.get("input_tokens") or 0),
        completion_tokens=int(raw.get("output_tokens") or 0),
        cached_tokens=int(raw.get("cache_read_input_tokens") or 0),
    )


# ---------------------------------------------------------------------------
# 错误归一化（详规 §4：五分支，对齐 openai/protocol.py 的 ErrorCode 家族）
# ---------------------------------------------------------------------------

# 上下文超限的文案标记。Anthropic 的超限报 ``invalid_request_error``，
# 只能靠 message 文案辨认——与 openai 侧 400 的辨认方式同源。
_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context length",
    "context_length",
    "prompt is too long",
)

# 认证类错误的 error.type 取值（官方枚举）。
_AUTH_ERROR_TYPES: frozenset[str] = frozenset(
    {"authentication_error", "permission_error"}
)


def _classify(
    *,
    error_type: str,
    message: str,
    status_code: int | None,
) -> ErrorCode:
    """五分支分类器。``error.type`` 是权威判据，HTTP 状态码为辅。

    分支顺序就是判定优先级（详规 §4）：

    1. 429 → ``rate_limit``；
    2. ``overloaded_error``（529）→ ``transient``；
    3. ``invalid_request_error`` 且 message 含超限文案 → ``context_overflow``；
    4. 认证失败（401/403 或 ``authentication_error`` / ``permission_error``）
       → ``auth``；
    5. 其余 → ``transient``。

    第 5 条为什么这么宽
        Anthropic 的错误枚举小而稳定，认不出的错误按"可重试的瞬时故障"
        处置偏向可用性；真正的不可重试错误（invalid_request / auth）都有
        显式分支拦在前面，不会落到兜底。
    """
    lowered = message.lower()
    if status_code == 429:
        return ErrorCode.RATE_LIMIT
    if error_type == "overloaded_error" or status_code == 529:
        return ErrorCode.TRANSIENT
    if error_type == "invalid_request_error" and any(
        marker in lowered for marker in _OVERFLOW_MARKERS
    ):
        return ErrorCode.CONTEXT_OVERFLOW
    if status_code in (401, 403) or error_type in _AUTH_ERROR_TYPES:
        return ErrorCode.AUTH
    return ErrorCode.TRANSIENT


def _extract_error(body_text: str) -> tuple[str, str, dict[str, Any]]:
    """从响应体里剥出 ``(error_type, message, raw)``。

    优先读 Anthropic 形状 ``{"type": "error", "error": {...}}``；
    读不到就用原始文本——**保留原文**，否则排查厂商差异时没线索。
    """
    message = body_text[:500]
    error_type = ""
    raw: dict[str, Any] = {}
    try:
        parsed = json.loads(body_text)
        if isinstance(parsed, dict):
            raw = parsed
            error = parsed.get("error")
            if isinstance(error, dict):
                error_type = str(error.get("type") or "")
                if error.get("message"):
                    message = str(error["message"])
            elif parsed.get("message"):
                message = str(parsed["message"])
    except json.JSONDecodeError:
        pass
    return error_type, message, raw


def _error_from_response(status_code: int, body_text: str) -> ProviderErrorPayload:
    """把 HTTP 错误响应转成 payload（建连 / 请求阶段的错误走这里）。"""
    error_type, message, raw = _extract_error(body_text)
    return ProviderErrorPayload(
        code=_classify(
            error_type=error_type, message=message, status_code=status_code
        ),
        message=message,
        raw=raw,
        status_code=status_code,
    )


def _error_from_stream_event(payload: dict[str, Any]) -> ProviderErrorPayload:
    """把流中途 ``error`` 事件的 data 转成 payload。

    与 HTTP 版的区别：**没有状态码**——全靠 ``error.type`` 分类，
    ``status_code`` 留 ``None``。``raw`` 保留事件原文，落盘后可排查。
    """
    error = payload.get("error")
    if isinstance(error, dict):
        error_type = str(error.get("type") or "")
        message = str(error.get("message") or "")
    else:
        error_type = ""
        message = str(payload)[:500]
    return ProviderErrorPayload(
        code=_classify(error_type=error_type, message=message, status_code=None),
        message=message or "(无 message 字段的流中错误)",
        raw=payload,
        status_code=None,
    )
