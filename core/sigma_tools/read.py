"""``read`` 工具：读取文本文件，支持行范围。

为什么行范围参数**必须有**
    架构方案 5.3 节：**截断与分页是配套的，只做截断不做分页会让 agent 卡死**。
    输出被截断之后，模型唯一能拿回完整内容的途径就是按行范围分批读。
    所以 ``start_line`` / ``end_line`` 不是"增强功能"，是截断策略的另一半。

输出带行号
    模型要能引用行号，否则 ``edit`` 的 ``old_string`` 定位只能靠猜，
    而猜错的代价是一次失败的工具调用加一轮纠错。
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._paths import resolve_path
from sigma_tools.truncate import truncate_output


class ReadParams(BaseModel):
    """``read`` 的参数。"""

    path: str = Field(description="要读取的文件路径。相对路径基于工作区根目录。")
    start_line: int | None = Field(default=None, ge=1, description="起始行号（1 起，含）")
    end_line: int | None = Field(default=None, ge=1, description="结束行号（1 起，含）")


class ReadTool(BaseTool):
    """读取文本文件。只读工具，允许与其它只读工具并发执行。"""

    name = "read"
    description = (
        "读取文本文件的内容。可用 start_line / end_line 指定行范围（1 起、含两端）。"
        "输出带行号。若输出提示被截断，请用行范围分批读取。"
    )
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return ReadParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行读取。

        **所有失败路径都返回 ``is_error=True``，不抛异常**（详规 3.6）——
        模型需要看到"文件不存在""不是文本""是目录"这些区别，才能换做法。
        """
        params = cast(ReadParams, args)
        path = resolve_path(ctx, params.path)

        if not path.exists():
            return ToolResult(
                content=[TextBlock(text=f"文件不存在：{path}")],
                details={"path": str(path)},
                is_error=True,
            )

        if path.is_dir():
            return ToolResult(
                content=[
                    TextBlock(
                        text=f"{path} 是目录不是文件。如需列出目录内容，请用 bash 的 ls。"
                    )
                ],
                details={"path": str(path)},
                is_error=True,
            )

        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                content=[
                    TextBlock(text=f"{path} 不是 UTF-8 文本文件，read 只处理文本。")
                ],
                details={"path": str(path)},
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                content=[TextBlock(text=f"读取 {path} 失败：{exc}")],
                details={"path": str(path)},
                is_error=True,
            )

        lines = raw.splitlines()
        total_lines = len(lines)

        # 行范围**越界必须报错**，不能静默返回空（2026-09-24 review 修复）：
        # 切片对非法范围天然返回 []，模型无法区分"文件是空的"和
        # "我给的行范围越界了"——两者的纠正动作完全不同，
        # 静默空输出会让它基于"文件为空"的错误前提继续行动（比如直接 write 覆盖）。
        if params.start_line is not None and params.start_line > total_lines:
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            f"start_line={params.start_line} 超出范围："
                            f"{path} 一共只有 {total_lines} 行。"
                            "请按总行数调整行范围后重试。"
                        )
                    )
                ],
                details={"path": str(path), "total_lines": total_lines},
                is_error=True,
            )
        if (
            params.start_line is not None
            and params.end_line is not None
            and params.start_line > params.end_line
        ):
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            f"行范围颠倒：start_line={params.start_line} "
                            f"> end_line={params.end_line}。请交换后重试。"
                        )
                    )
                ],
                details={"path": str(path), "total_lines": total_lines},
                is_error=True,
            )

        start_index = (params.start_line or 1) - 1
        end_index = params.end_line if params.end_line is not None else total_lines
        selected = lines[start_index:end_index]

        numbered = "\n".join(
            f"{start_index + offset + 1}\t{line}" for offset, line in enumerate(selected)
        )

        result = truncate_output(numbered)
        text = result.text
        if result.truncated:
            # 明确告诉模型"你拿到的是文件的一部分"，否则它会以为文件就这么长
            text = (
                f"[文件 {path} 共 {total_lines} 行；本次返回第 "
                f"{start_index + 1}–{min(end_index, total_lines)} 行，"
                f"其中超过 8 KB 的部分已截断。"
                f"请用 start_line / end_line 分批读取。]\n{text}"
            )

        details: dict[str, Any] = {
            "path": str(path),
            "total_lines": total_lines,
            "returned_lines": len(selected),
            "truncated": result.truncated,
            "total_bytes": result.total_bytes,
        }
        return ToolResult(content=[TextBlock(text=text)], details=details)
