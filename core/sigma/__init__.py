"""sigma —— L5：产品壳。

职责
    CLI / REPL / 一次性模式，以及 SDK 入口 ``create_session()``。
    这一层只做组装，不承载任何 agent 逻辑。

允许依赖
    sigma_tools, sigma_session, sigma_agent, sigma_ai

实现状态
    P0 时仅包声明与占位 CLI——已失效的历史状态，
    现 CLI/REPL/SDK 完整可用（本行 2026-09-27 评审时补注日期）。
"""

from __future__ import annotations

__version__ = "0.0.1"

__all__: list[str] = ["__version__"]
