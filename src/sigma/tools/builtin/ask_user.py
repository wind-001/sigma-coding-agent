"""``ask_user`` —— 把"方向决策"交还给用户:选项列表 + 推荐项。

需求(星辰,2026-09-26,参照 AskUserQuestion 交互):工作执行过程中,
进度涉及到**用户决定方向**时,模型不应擅自替人拍板——给出编号选项列表、
标明推荐项,等用户挑选;结果作为工具结果回到模型,继续执行。

为什么是工具而不是钩子
    审批钩子是**harness 主动**拦(危险/越界,时机由代码定);
    ask_user 是**模型主动**问(方向分叉,时机由任务定)。
    两者共用同一条交互通道(``ToolContext.ask``),但触发方不同。

答案的三种形态(2026-10-04 扩,星辰"留自由选择空间"):
    1. **命中候选**——常规路径,按所选继续;
    2. **非空但不在候选里**——视为**自由输入**,如实转给模型
       (工作台问题卡的文本框;市面成熟问询 UI 的标配,候选之外
       用户常有第四条路);
    3. **空串/哨兵/EOF**——"未作选择":哨兵 = 用户在 UI 上**显式忽略**
       (卡片上的"忽略本次"),空串 = 超时/流关闭。前者告诉模型
       "用户看到了但不想选,自行决定";后者回退推荐项并如实注明。

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
#: 工作台"忽略本次"按钮的哨兵:显式告知模型"用户看到了但不想选"。
#: 值刻意怪异——自由输入撞上它的概率趋近于零。
IGNORE_SENTINEL = "__sigma_ignore__"


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
        "等待用户挑选后按选择继续。用户也可能自由输入候选之外的答案,"
        "或忽略本次提问——两种都按用户意愿继续。"
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
        if answer == IGNORE_SENTINEL:
            # 显式忽略:不替用户挑,模型自行决定后续路径。
            # 措辞必须**禁止重问**——2026-10-04 实测,忽略后模型(深度思考里
            # 明确计划"连续调用三次 ask_user")无视忽略又问了两轮,每轮阻塞
            # 等待,用户体感就是"卡住"。
            return ToolResult(
                content=[
                    TextBlock(
                        text="用户看到了选项但选择了忽略本次提问:未作选择。"
                        "**不要再次调用 ask_user 重复同样的或相似的问题**——"
                        "请基于现有信息自行决定后续路径并直接继续执行。"
                    )
                ],
                details={"choice_index": None, "choice": None, "note": "用户忽略本次"},
            )
        if answer.strip() != "":
            # 自由输入:候选之外的第四条路,如实转达,不回退推荐项。
            custom = answer.strip()
            return ToolResult(
                content=[TextBlock(text=f"用户自行输入(不在候选中):{custom}")],
                details={
                    "choice_index": None,
                    "choice": custom,
                    "note": "自由输入",
                },
            )
        # EOF / 超时 / 空串:回退到推荐项,但如实说明"没有收到有效选择"
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
