"""``grep`` 工具：按正则在文件内容中搜索。

输出必须**带文件名与行号**（详规 3.5）
    模型拿到 ``path:line:text`` 才能直接跟上 ``read(start_line=...)`` 或
    ``edit``——没有行号，定位只能靠反复整文件读，那是 token 的无谓消耗。

明确不做的（P1 范围）
    - 不读 ``.gitignore``，也不做排除规则——只按名字跳过 ``.git`` 目录
      （里面是二进制对象，搜它纯是噪音）。
    - ``glob`` 只按**文件名**匹配（如 ``*.py``），不做路径模式。
    这些在 P1 是如实声明的适用范围，不是将来补——评测任务不依赖它们。

二进制 / 非 UTF-8 文件的处理
    跳过，但**计数并在结果尾部报告**——静默跳过会让模型以为
    "搜过了，没有"，而实际是"这个文件根本没被搜到"。
    两者对模型是不同的结论。

单行截断到 240 字符的原因
    一行压缩过的 JS / base64 可以有数 MB。整体截断（8 KB）之前，
    单行就该先截——否则第一个命中就能吃光整个输出预算，
    后面的命中全部消失。
"""

from __future__ import annotations

import re
from typing import Any, cast
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._paths import resolve_path
from sigma_tools.truncate import truncate_output

MAX_LINE_CHARS = 240

SKIP_DIRS = {".git"}


class GrepParams(BaseModel):
    """``grep`` 的参数。"""

    pattern: str = Field(min_length=1, description="正则表达式（Python re 语法）。")
    path: str | None = Field(
        default=None,
        description="搜索范围：文件或目录。相对路径基于工作区根目录；不传则搜整个工作区。",
    )
    glob: str | None = Field(
        default=None,
        description="只搜文件名匹配此模式（如 '*.py'）的文件。不传则搜所有文本文件。",
    )


class GrepTool(BaseTool):
    """内容搜索。只读工具，允许与其它只读工具并发执行。"""

    name = "grep"
    description = (
        "用正则表达式在文件内容中搜索，返回 文件路径:行号:匹配行。"
        "可用 glob 参数（如 '*.py'）按文件名过滤。非 UTF-8 文件会被跳过并计数。"
    )
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return GrepParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行搜索。正则非法 / 路径不存在返回 ``is_error=True``；**无匹配不是错误**。"""
        params = cast(GrepParams, args)

        try:
            regex = re.compile(params.pattern)
        except re.error as exc:
            return ToolResult(
                content=[
                    TextBlock(
                        text=f"正则表达式非法：{exc}\n请修正 pattern 后重试。"
                    )
                ],
                details={"pattern": params.pattern},
                is_error=True,
            )

        root = resolve_path(ctx, params.path) if params.path else ctx.workspace_root
        if not root.exists():
            return ToolResult(
                content=[TextBlock(text=f"搜索路径不存在：{root}")],
                details={"path": str(root)},
                is_error=True,
            )

        targets = (
            [root]
            if root.is_file()
            else sorted(p for p in root.rglob("*") if p.is_file())
        )

        lines: list[str] = []
        match_count = 0
        searched = 0
        skipped_binary = 0

        for file_path in targets:
            # 只按名字跳过 .git——目录排除规则（.gitignore）是 P1 明确不做的
            if any(part in SKIP_DIRS for part in file_path.parts):
                continue
            if params.glob is not None and not fnmatch(file_path.name, params.glob):
                continue

            try:
                # universal newlines：与 read 一致，行号按 \n 计
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped_binary += 1
                continue
            except OSError:
                # 读不了（权限等）与二进制同理：不能搜 ≠ 不存在
                skipped_binary += 1
                continue

            searched += 1
            rel = file_path.relative_to(ctx.workspace_root) if file_path.is_relative_to(ctx.workspace_root) else file_path
            # 输出统一用正斜杠：与 bash / read 的行号引用一致，
            # Windows 上也保持同一形态（模型不需要知道运行在哪个系统上）
            shown = rel.as_posix()
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line) is None:
                    continue
                match_count += 1
                display = line.strip()[:MAX_LINE_CHARS]
                suffix = " …" if len(line.strip()) > MAX_LINE_CHARS else ""
                lines.append(f"{shown}:{line_no}:{display}{suffix}")

        details: dict[str, Any] = {
            "pattern": params.pattern,
            "matches": match_count,
            "files_searched": searched,
            "files_skipped": skipped_binary,
        }

        if match_count == 0:
            # 无匹配是合法结果，不是错误——模型据此换 pattern 或扩大范围
            tail = (
                f"（另有 {skipped_binary} 个文件因非 UTF-8 或不可读被跳过）"
                if skipped_binary
                else ""
            )
            return ToolResult(
                content=[
                    TextBlock(
                        text=f"没有匹配：pattern={params.pattern!r}，"
                        f"共搜索 {searched} 个文件。{tail}"
                    )
                ],
                details=details,
            )

        body = "\n".join(lines)
        result = truncate_output(body)
        details["truncated"] = result.truncated
        tail_note = (
            f"（另有 {skipped_binary} 个文件因非 UTF-8 或不可读被跳过）"
            if skipped_binary
            else ""
        )
        header = f"共 {match_count} 个匹配（{searched} 个文件）：{tail_note}"
        if result.truncated:
            header += (
                "\n[结果被截断，请缩小 pattern、指定更精确的 path 或用 glob 过滤。]"
            )
        return ToolResult(
            content=[TextBlock(text=f"{header}\n\n{result.text}")], details=details
        )
