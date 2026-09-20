"""``edit`` 工具：精确替换文件中的一段文本。

三态规则（详规 3.5，``P1-最小闭环.md`` R4 已定）
    ``old_string`` 出现 0 次   → 报错，提示先 read 确认当前内容
    ``old_string`` 出现 1 次   → 执行替换
    ``old_string`` 出现 N > 1 次 → 报错，要求提供更大上下文消除歧义

**绝不提供"替换全部"**（详规 3.5 明确不做清单第 1 项）
    静默替换多处会产出一个"看起来改对了"的文件，而错误要到测试才暴露。
    这与 ``write`` 拒绝自动创建父目录是同一条原则：
    **有歧义时宁可报错，不要猜。**

行尾（CRLF）为什么要特殊处理
    ``read`` 用 universal newlines 读文件——模型看到的内容里行尾是 ``\\n``，
    所以模型给的 ``old_string`` 也只会含 ``\\n``。若直接对原始字节做替换，
    在 CRLF 文件上**永远匹配不到**，症状是"0 次匹配"却不指向根因。

    做法：读入时归一化为 ``\\n`` 再匹配，写回时按**文件原有的行尾风格**还原。
    模型看到什么就匹配什么，文件字节不被悄悄改变。
"""

from __future__ import annotations

import difflib
from typing import Any, cast

from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._paths import resolve_path
from sigma_tools.truncate import truncate_output


class EditParams(BaseModel):
    """``edit`` 的参数。"""

    path: str = Field(description="目标文件路径。相对路径基于工作区根目录。")
    old_string: str = Field(min_length=1, description="要被替换的原文。必须在文件中恰好出现一次。")
    new_string: str = Field(description="替换后的文本。传空字符串表示删除这段内容。")


def _detect_newline(raw: bytes) -> str:
    """检测文件原有的行尾风格。

    按 CRLF → CR → LF 的顺序判定。写回时用同一风格还原，
    避免"改了一行、全文件行尾被换掉"的隐性 diff。
    """
    if b"\r\n" in raw:
        return "\r\n"
    if b"\r" in raw:
        return "\r"
    return "\n"


class EditTool(BaseTool):
    """精确替换。属写工具，在批次执行时**严格顺序执行**（详规 3.8）。"""

    name = "edit"
    description = (
        "把文件中的一段文本精确替换为另一段。old_string 必须在文件中恰好出现一次，"
        "出现多次会被拒绝——请扩大上下文使其唯一。修改已有文件时优先用本工具而不是 write。"
    )
    read_only = False

    @property
    def params(self) -> type[BaseModel]:
        return EditParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行替换。

        与 read / write 一致：**所有失败路径都返回 ``is_error=True``，不抛异常**。
        模型需要区分"文件不存在""没匹配上""匹配了多次"这三种失败，
        它们的纠法各不相同。
        """
        params = cast(EditParams, args)
        path = resolve_path(ctx, params.path)

        if not path.exists():
            return ToolResult(
                content=[TextBlock(text=f"文件不存在：{path}")],
                details={"path": str(path)},
                is_error=True,
            )
        if path.is_dir():
            return ToolResult(
                content=[TextBlock(text=f"{path} 是目录，edit 只处理文件。")],
                details={"path": str(path)},
                is_error=True,
            )

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ToolResult(
                content=[TextBlock(text=f"读取 {path} 失败：{exc}")],
                details={"path": str(path)},
                is_error=True,
            )

        newline = _detect_newline(raw)
        try:
            # universal newlines：\r\n / \r 都归一化为 \n，与 read 给模型看的一致
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                content=[TextBlock(text=f"{path} 不是 UTF-8 文本文件，edit 只处理文本。")],
                details={"path": str(path)},
                is_error=True,
            )
        content = content.replace("\r\n", "\n").replace("\r", "\n")

        # 三态规则的核心：先数出现次数，1 次才动手
        occurrences = content.count(params.old_string)
        if occurrences == 0:
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            f"old_string 在 {path} 中出现 0 次，未做任何修改。\n"
                            "请先 read 确认当前内容（注意空格与缩进也要完全一致），再重试。"
                        )
                    )
                ],
                details={"path": str(path), "occurrences": 0},
                is_error=True,
            )
        if occurrences > 1:
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            f"old_string 在 {path} 中出现 {occurrences} 次，已拒绝替换。\n"
                            "多匹配时静默全部替换会产生看起来正确实则损坏的文件。"
                            "请在 old_string 中包含更多上下文使其唯一，再重试。"
                        )
                    )
                ],
                details={"path": str(path), "occurrences": occurrences},
                is_error=True,
            )

        new_content = content.replace(params.old_string, params.new_string, 1)
        if new_content == content:
            # count==1 且内容没变只可能是 old_string == new_string——
            # 一次什么都不做的 edit 几乎必然是模型搞错了参数
            return ToolResult(
                content=[
                    TextBlock(text="old_string 与 new_string 相同，这次编辑是空操作，未写入。")
                ],
                details={"path": str(path)},
                is_error=True,
            )

        try:
            # 按**文件原有风格**还原行尾后写回
            path.write_bytes(new_content.replace("\n", newline).encode("utf-8"))
        except OSError as exc:
            return ToolResult(
                content=[TextBlock(text=f"写入 {path} 失败：{exc}")],
                details={"path": str(path)},
                is_error=True,
            )

        # unified diff 让模型看到"实际改了什么"，行尾统一按 \n 展示
        diff_lines = list(
            difflib.unified_diff(
                content.splitlines(),
                new_content.splitlines(),
                fromfile=f"a/{path.name}",
                tofile=f"b/{path.name}",
                lineterm="",
            )
        )
        diff_text = "\n".join(diff_lines)
        result = truncate_output(diff_text)

        details: dict[str, Any] = {
            "path": str(path),
            "replaced": True,
            "occurrences": 1,
            "old_bytes": len(raw),
            "new_bytes": len(new_content.replace("\n", newline).encode("utf-8")),
            "truncated": result.truncated,
        }
        return ToolResult(
            content=[TextBlock(text=f"已替换 {path} 中的 1 处。\n\n{result.text}")],
            details=details,
        )
