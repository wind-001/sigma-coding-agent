"""sigma_tools —— L4：内置工具实现。

职责
    read / write / edit / bash / grep 五个内置工具，以及输出截断与分页。
    （grep 为 2026-09-20 新增，见 docs/plans/P1-最小闭环.md 第 7.1 节）
    与扩展工具共用 sigma_agent 中的同一套注册表接口，
    因此「用扩展替换内置工具」是免费的。

允许依赖
    sigma_agent, sigma_ai

实现状态
    2026-09-20：read / write / edit / bash / grep 五个工具全部实现。
    edit 遵循三态规则（多匹配拒绝、绝不替换全部）；
    bash 强制超时、无任何命令过滤（详规 R1）；
    grep 返回 文件:行号:文本，跳过 .git 与非 UTF-8 文件（计数上报）。
"""

from __future__ import annotations

__all__: list[str] = []
