"""sigma_session —— L3：会话与上下文内核。

职责
    会话树（JSONL，id + parentId）、上下文组装与预算、压缩、
    项目指令与技能资源加载、扩展装配。
    对应 docs/architecture.md 的 ``sigma_session`` 层，
    也对应手绘架构图中「pi-coding-agent（产品内核）」那一格。

允许依赖
    sigma_agent, sigma_ai

实现状态
    P0：仅包声明，无实现。实现计划见 docs/plans/。
"""

from __future__ import annotations

__all__: list[str] = []
