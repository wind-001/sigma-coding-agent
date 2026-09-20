"""sigma_ai —— L1：Provider 抽象层。

职责
    统一多家 LLM 供应商的消息结构、工具调用、流式事件、用量统计与错误语义。
    对应 docs/architecture.md 的 ``sigma_ai`` 层。

允许依赖
    无。本层不得引用任何其他 sigma 内部包。

实现状态
    P0：仅包声明，无实现。实现计划见 docs/plans/。
"""

from __future__ import annotations

__all__: list[str] = []
