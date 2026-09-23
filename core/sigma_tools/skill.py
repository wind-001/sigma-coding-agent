"""`load_skill` —— 把技能正文拉进上下文。

**渐进披露的"后半截"**：常驻区里只有技能目录（name + description + location），
正文在那个目录里被"点名"之后才进来。所以这个工具的 description **本身也要省**——
它进常驻区，每轮都要重付一次。

它**不自己去找技能**：技能列表由调用方构造时传入。
理由与 ``read`` / ``write`` 不自己去猜工作区根目录是同一条判据：
**谁决定策略，谁传参**。技能放在哪个目录是产品壳的决定，
工具层不该有自己的意见，更不该 import ``sigma_session``（兄弟层契约）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.skills import DEFAULT_MAX_BODY_TOKENS, SkillMeta, load_body
from sigma_agent.types import ToolResult
from sigma_ai.messages import TextBlock

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sigma_agent.types import ToolContext


class LoadSkillParams(BaseModel):
    """参数：**只要一个名字**。

    刻意不接受"路径"或"部分内容匹配"：
    名字来自常驻区那份目录，是模型唯一有把握给出的东西。
    允许路径意味着模型可以绕过目录去读任意文件——
    那是 ``read`` 工具的职责，混进来只会让两个工具的边界变模糊。
    """

    name: str = Field(
        min_length=1,
        description="技能名，取自系统提示词里「可用技能」列表中的名字。",
    )


class LoadSkillTool(BaseTool):
    """按名加载技能正文。只读工具，可与其它只读工具并发。"""

    name = "load_skill"
    description = (
        "加载一个技能的完整说明。**只在确实需要时才调用**——"
        "正文会占用上下文。技能名见系统提示词里的「可用技能」列表。"
    )
    read_only = True

    def __init__(
        self,
        *,
        skills: Sequence[SkillMeta],
        max_tokens: int = DEFAULT_MAX_BODY_TOKENS,
    ) -> None:
        self._by_name = {skill.name: skill for skill in skills}
        self._max_tokens = max_tokens

    @property
    def params(self) -> type[BaseModel]:
        return LoadSkillParams

    @property
    def skill_names(self) -> list[str]:
        """已知技能名（排序）。给横幅与测试用。"""
        return sorted(self._by_name)

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(LoadSkillParams, args)
        skill = self._by_name.get(params.name)

        if skill is None:
            # **失败时要把可用的名字列出来。**
            #
            # 模型手里只有常驻区那份目录，而它可能是"记错了名字"
            # （大小写、单复数、把描述里的词当成名字）。
            # 只回一句"没有这个技能"，模型只能瞎猜着重试，
            # 而每次重试都是一次完整的模型调用 —— **把名字列出来就省掉这些轮**。
            available = "、".join(self.skill_names) or "（当前没有任何技能）"
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            f"没有名为 {params.name!r} 的技能。"
                            f"可用的技能是：{available}。"
                            "请从这些名字里选一个，或不用技能继续。"
                        )
                    )
                ],
                details={"requested": params.name, "available": self.skill_names},
                is_error=True,
            )

        result = load_body(skill, max_tokens=self._max_tokens)
        if not result.text:
            # 空文本有**三种**原因，而它们要指向完全不同的排查方向。
            # 一开始这里只有一句话："正文读不出来（权限 / 编码 / 文件被删）"——
            # 于是"预算小到连截断说明都放不下"也被报成了这个，
            # **人和模型都会去查文件系统，而真正要改的是预算**。
            # 症状不指向根因，正是本项目最不能接受的那种失败。
            if result.truncated:
                # truncated=True 且正文为空 ⇒ 丢过东西，但没地方写说明。
                return ToolResult(
                    content=[
                        TextBlock(
                            text=(
                                f"技能 {skill.name} 的正文约 {result.original_tokens} token，"
                                f"当前预算（{self._max_tokens} token）里连"
                                "「已截断」这句说明都放不下，因此没有返回正文。"
                                f"请直接用 read 工具读取 {skill.location}，"
                                "或调大该工具的正文预算。"
                            )
                        )
                    ],
                    details={
                        "name": skill.name,
                        "location": skill.location,
                        "original_tokens": result.original_tokens,
                        "max_tokens": self._max_tokens,
                        "reason": "预算装不下截断说明",
                    },
                    is_error=True,
                )
            # truncated=False 且正文为空 ⇒ 真的没内容：文件读不出来
            # （权限 / 编码 / 被删），或文件里只有 frontmatter。
            # 这两种在 ``TruncatedText`` 上不可区分，**处置也一样**，
            # 所以合成一句——但文案不咬定其中某一个原因。
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            f"技能 {skill.name} 没有正文（文件：{skill.location}）——"
                            "可能读不出来，也可能文件里只有 frontmatter。"
                            "请改用别的方式完成任务，或直接读该文件确认。"
                        )
                    )
                ],
                details={
                    "name": skill.name,
                    "location": skill.location,
                    "reason": "正文为空",
                },
                is_error=True,
            )

        # 正文前加一行"这是什么"，否则模型拿到一段没有标题的规范，
        # 分不清它是技能、是工具结果、还是用户说的话。
        header = f"[技能 {skill.name} 的完整说明"
        header += "（已截断）]" if result.truncated else "]"
        return ToolResult(
            content=[TextBlock(text=f"{header}\n\n{result.text}")],
            details={
                "name": skill.name,
                "location": skill.location,
                "tokens": result.tokens,
                "original_tokens": result.original_tokens,
                "truncated": result.truncated,
            },
        )
