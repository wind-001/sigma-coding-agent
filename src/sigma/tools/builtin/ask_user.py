"""``ask_user`` —— 把"方向决策"交还给用户:选项列表 + 推荐项。

需求(星辰,2026-09-26,参照 AskUserQuestion 交互):工作执行过程中,
进度涉及到**用户决定方向**时,模型不应擅自替人拍板——给出编号选项列表、
标明推荐项,等用户挑选;结果作为工具结果回到模型,继续执行。

为什么是工具而不是钩子
    审批钩子是**harness 主动**拦(危险/越界,时机由代码定);
    ask_user 是**模型主动**问(方向分叉,时机由任务定)。
    两者共用同一条交互通道(``ToolContext.ask``),但触发方不同。

无交互通道时怎么办(评测 / 管道 / CI)
    ``ctx.ask is None`` → **自动采用推荐项**(没给推荐就用第一项),
    结果里显式注明"[非交互模式] 自动采用推荐项"——执行不断,
    但这个选择是可见、可审计的,不是悄悄替用户决定。

description 为什么刻意写短
    它进常驻区,每轮都重付一次(load_skill 同一条纪律)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field

from sigma.tools.base import BaseTool
from sigma.agent.types import ToolResult
from sigma.providers.messages import TextBlock

if TYPE_CHECKING:
    from sigma.agent.types import ToolContext

TOOL_NAME = "ask_user"

MAX_OPTIONS = 6
_AUTO_NOTE = "[非交互模式] 自动采用推荐项"
_FALLBACK_NOTE = "[未收到有效选择] 自动采用推荐项"


class AskUserParams(BaseModel):
    """参数:问题 + 选项 + 推荐下标(0 起)。

    ``recommended_index`` 是**推荐**,不是默认强加——交互模式下用户仍可选
    任何一项;它只在"无人可问"时决定自动回退到哪一项。
    """

    question: str = Field(min_length=1, description="要请用户决定的问题")
    options: list[str] = Field(
        min_length=2, max_length=MAX_OPTIONS, description="候选方向,2 到 6 项"
    )
    recommended_index: int | None = Field(
        default=None, ge=0, description="推荐项的下标(0 起);不推荐则不填"
    )


class AskUserTool(BaseTool):
    """向用户展示选项列表并等待挑选。``read_only=True``:不碰文件、不持锁。"""

    name = TOOL_NAME
    description = (
        "需要用户在几个方向里做选择时调用:给出问题、候选选项与推荐项,"
        "等待用户挑选后按选择继续。"
    )
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return AskUserParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(AskUserParams, args)
        options = list(params.options)
        recommended = params.recommended_index
        if recommended is not None and not (0 <= recommended < len(options)):
            recommended = None

        if ctx.ask is None:
            chosen = recommended if recommended is not None else 0
            return _result(options, chosen, note=_AUTO_NOTE)

        answer = await ctx.ask(params.question, options, recommended)
        if answer in options:
            index = options.index(answer)
            note = (
                "与推荐一致"
                if recommended is not None and index == recommended
                else f"推荐项是:{options[recommended]}"
                if recommended is not None
                else ""
            )
            return _result(options, index, note=note)
        # EOF / 非法输入:回退到推荐项,但如实说明"没有收到有效选择"
        chosen = recommended if recommended is not None else 0
        return _result(options, chosen, note=_FALLBACK_NOTE)


def _result(options: list[str], index: int, *, note: str) -> ToolResult:
    lines = [f"用户选择了 选项{index + 1}:{options[index]}"]
    if note:
        lines.append(note)
    return ToolResult(
        content=[TextBlock(text="\n".join(lines))],
        details={"choice_index": index, "choice": options[index], "note": note},
    )
