"""``write`` 工具：整文件写入（覆盖）。

为什么父目录不存在时**不自动创建**
    自动创建看起来"更友好"，但它会把一个**路径写错**变成一次静默的
    错误创建：模型本意是改 ``src/a.py``，写错了变成 ``srcc/a.py``，
    而目录被默默创建出来，于是没有任何一步报错——直到测试找不到文件。

    报错则立刻把这个错误摆到模型面前，它自己会纠正。
    这与 ``edit`` 的"多匹配必须拒绝"是同一条原则：
    **有歧义时宁可报错，不要猜**（详规 3.5）。
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._paths import resolve_path


class WriteParams(BaseModel):
    """``write`` 的参数。"""

    path: str = Field(description="目标文件路径。相对路径基于工作区根目录。")
    content: str = Field(description="要写入的完整内容（覆盖原内容）。")


class WriteTool(BaseTool):
    """整文件写入。属写工具，在批次执行时**严格顺序执行**（详规 3.8）。"""

    name = "write"
    description = (
        "把内容写入文件，覆盖原内容。父目录不存在时会报错，不会自动创建。"
        "若是修改已有文件的一小部分，请用 edit 而不是 write。"
    )
    read_only = False

    @property
    def params(self) -> type[BaseModel]:
        return WriteParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(WriteParams, args)
        path = resolve_path(ctx, params.path)

        if path.is_dir():
            return ToolResult(
                content=[TextBlock(text=f"{path} 是目录，不能写入。")],
                details={"path": str(path)},
                is_error=True,
            )

        if not path.parent.exists():
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            f"父目录不存在：{path.parent}\n"
                            "**不会自动创建目录。** 请确认路径是否正确；"
                            "若确实需要新目录，请先用 bash 执行 mkdir -p。"
                        )
                    )
                ],
                details={"path": str(path), "missing_parent": str(path.parent)},
                is_error=True,
            )

        existed = path.exists()
        old_bytes = path.stat().st_size if existed else 0
        new_bytes = len(params.content.encode("utf-8"))

        try:
            path.write_text(params.content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                content=[TextBlock(text=f"写入 {path} 失败：{exc}")],
                details={"path": str(path)},
                is_error=True,
            )

        verb = "覆盖" if existed else "新建"
        summary = f"{verb} {path}，现在 {new_bytes} 字节"
        if existed:
            summary += f"（原先 {old_bytes} 字节）"

        details: dict[str, Any] = {
            "path": str(path),
            "created": not existed,
            "old_bytes": old_bytes,
            "new_bytes": new_bytes,
        }
        return ToolResult(content=[TextBlock(text=summary)], details=details)
