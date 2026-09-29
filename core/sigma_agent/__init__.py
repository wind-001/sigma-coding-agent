"""sigma_agent —— L2：Agent 循环引擎。

职责
    agent loop、工具注册表与执行管道、钩子总线、状态管理、checkpoint。
    对应 docs/architecture.md 的 ``sigma_agent`` 层。

允许依赖
    sigma_ai

实现状态
    P0 时仅包声明——已失效的历史状态，现 loop/注册表/钩子/
    checkpoint 均已落地（本行 2026-09-27 评审时补注日期）。
"""

from __future__ import annotations

__all__: list[str] = []
