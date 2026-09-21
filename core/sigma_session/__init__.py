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

    2026-09-21：P2 逐步落地。
    - ``context.py`` 内存版上下文（常驻区指纹断言，P1 起）；
    - ``store.py`` 会话落盘：一会话一 JSONL 文件，坏行跳过 + warning；
    - ``tree.py`` 会话树：``append`` / ``append_to``（分支）/ ``set_head``（回滚）
      / ``path_to``（含环检测与断链检测）。

    **没有 ``base.py``**：P2 详规 Q4 决定不抽 ``BaseStore``——本阶段只有 JSONL
    一个实现，按"只有一个子类的抽象基类 = 纯间接"的既有判据，
    现在抽它等于给唯一实现加一层纯间接。等第二个后端出现再抽。
    架构 3.1 的目录树里写了 ``base.py``，所以在这里记明：**没做是决定，不是遗漏。**

    仍有未落地：``compact.py``（压缩）、``resources.py``（``AGENTS.md`` 注入）。
"""

from __future__ import annotations

__all__: list[str] = []
