"""OpenAI 兼容协议的**协议细节**：finish_reason 映射、用量解析、错误体解析。

为什么单独成文件
    这三件事的共同点是**纯函数**——入参出参，不碰网络、不持有状态。
    因此它们可以被单测直接打，不必起 HTTP。

    从 `openai_compat.py` 拆出来之前，它们和 HTTP 流式循环挤在同一个
    534 行的文件里（见 docs/plans/P1-sigma_ai重构-详规.md 子项 B）。

各厂商的差异都集中在这里
    `finish_reason` 取值、`usage` 字段位置、`cached_tokens` 的嵌套层级，
    各家的"OpenAI 兼容"程度不一样。把这些差异收在一个文件里，
    排查时只需要看这一处。
"""

from __future__ import annotations

import json
from typing import Any

from sigma_ai.errors import (
    ProviderError,
    ProviderErrorPayload,
    classify_http_status,
)
from sigma_ai.messages import Usage


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
        # ``or 0`` 不是摆设：``.get(key, 0)`` 只在键**缺失**时给默认值，
        # 厂商显式发 ``"prompt_tokens": null`` 时拿到的是 None，``int(None)``
        # 直接抛 TypeError。三个字段必须同一条防线（cached 先修的，这两个补上）。
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
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
