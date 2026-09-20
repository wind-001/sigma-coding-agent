"""工具共用的路径解析。

**这不是安全边界。**

    D5 的三层软边界（工作区约束 / 影子 checkpoint / 钩子规则）在 P1 一层都没落地，
    见详规 0.1 节代价第 3 条与风险 R1。本模块只负责"相对路径有一个确定的基准目录"，
    **不假装能挡住路径逃逸**。

为什么不顺手加一句 ``is_relative_to(workspace_root)`` 检查
    那是**名义防护**：它挡不住符号链接，更挡不住 ``bash``，
    却会让代码看起来"已经有保护了"。
    本项目已固化的纪律——**名义门槛比没有门槛更坏**（ruff 那次就是这么删的）。

    真做边界是 P3 的钩子体系，届时必须连符号链接与真实路径一起处理。
    现在加半成品，P3 一定推翻重写，而中间的这段时间里它只会制造虚假安全感。
"""

from __future__ import annotations

from pathlib import Path

from sigma_agent.types import ToolContext


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    """把工具参数里的路径解析成绝对路径。

    绝对路径原样返回；相对路径基于 ``ctx.workspace_root`` 拼接。

    不用 ``Path.resolve()``：它会解析符号链接，且对不存在的路径行为随版本变动。
    工具需要的是"用户说的那个路径在哪儿"，不是"它真实指向哪儿"——
    后者是 P3 安全边界该操心的事情。
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return ctx.workspace_root / candidate
