"""P0 详规「已知边界①」的门槛测试:一轮聚合后"零文本+零有效调用"不得落成空 content。

缺陷锚点
    ``docs/plans/Review-2026-09-26-P0缺陷修复-详规.md`` 第 81 行「已知边界」①:

    > 全部调用都拼装失败且无文本时,assistant content 为空(``convert.py`` 已
    > 映射为 ``null``)——真实 API 对"空 assistant 且无 tool_calls"可能仍拒。

    复现(修复前):一轮里模型只发了一个 JSON 截断的工具调用、没有文本 →
    ``_stream_model`` 聚合出的 ``AssistantMessage.content == []`` →
    ``message_to_openai`` 产出 ``{"role": "assistant", "content": None}``
    且无 ``tool_calls``——下一轮请求可能被真实 API 直接拒收;
    同时审计链上这一轮"什么都不剩"(assistant 没有任何可见内容)。

修复方案与理由(为什么选 loop 侧,而不是 convert 侧)
    选 **(a) loop 侧**:``_stream_model`` 聚合成"零文本+零有效调用"时,
    给 content 补一条可见的 ``TextBlock`` 系统注记。

    1. **它一次修掉缺陷的两半**。content 非空 → 协议载荷合法(content 不是
       null);审计链上这一轮也留下了"发生了什么"的可见痕迹。
       convert 侧的方案 (b)(空 content 映射为空串)只能救协议那一半:
       审计链上这一轮仍然"什么都不剩",而且空串 content 本身同样可能被
       部分 OpenAI 兼容端点拒收——把 null 换成 "" 只是换一种被拒的方式。
    2. **知识的位置**。"这一轮有 N 个调用拼装失败"是 loop 聚合时才知道的
       事实;``convert.py`` 是纯函数,拿到 ``content=[]`` 时这个信息已经丢了,
       它没有依据去捏造内容。在纯函数里凭空造文本违反本层的职责边界。
    3. **与既有纪律同族**:unparsed 调用已有"尾部 user 系统注记"
       (``_failure_message``,P0 修复 2026-09-26)——「失败不伪装成合法调用,
       但也不许静默消失」。本注记是同一条纪律在 assistant 消息上的延伸。
    4. 改 convert 的映射会**静默改写所有**空 assistant 消息的协议形状
       (含手工构造的历史消息),范围远大于缺陷本身;loop 侧补注记只影响
       "真的聚合出了空轮"这一条路径。

覆盖两个入口(判据都是"零文本+零有效调用"):
    1. 全部工具调用拼装失败(unparsed)且无文本——缺陷本体;
    2. 模型什么都没产(无文本、无调用)——同一聚合条件下的孪生路径,
       落盘的 assistant 同样会 content=[],协议风险相同。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field

from sigma.agent.messages import LlmMessageWrapper
from sigma.providers.base import CancelToken
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import AssistantMessage, TextBlock, UserMessage
from sigma.providers.openai.convert import message_to_openai
from sigma.providers.stamps import from_epoch as ts
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.base import BaseTool
from sigma.tools.registry import ToolRegistry
from sigma.agent.types import ToolContext, ToolResult

FIXED_TIME = ts(1_700_000_000)

# 注记的锚定文案:断言只认这个稳定前缀,不绑全文措辞
ANNOTATION_MARK = "系统注记"


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class EchoParams(BaseModel):
    message: str = Field(description="要回显的内容")


class EchoTool(BaseTool):
    name = "echo"
    description = "回显 message"
    read_only = True

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(EchoParams, args)
        self.seen.append(params.message)
        return ToolResult(content=[TextBlock(text=f"echo: {params.message}")])


def _make_loop(
    rounds: list[list[dict[str, Any]]], *, max_rounds: int = 5
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentLoop(
        provider=FakeProvider.from_rounds(rounds),
        registry=registry,
        model="fake",
        workspace_root=str(Path.cwd()),
        max_rounds=max_rounds,
        signal=_NeverCancelled(),
        clock=lambda: FIXED_TIME,
    )


def _history() -> list[LlmMessageWrapper]:
    return [
        LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="干活", timestamp=FIXED_TIME),
        )
    ]


def _assistants_of(messages: list[Any]) -> list[AssistantMessage]:
    """按落盘顺序抽出本轮产出里的全部 assistant 消息。"""
    return [
        cast(AssistantMessage, m.message)
        for m in messages
        if isinstance(m, LlmMessageWrapper)
        and isinstance(m.message, AssistantMessage)
    ]


@pytest.mark.asyncio
async def test_all_unparsed_round_with_no_text_leaves_visible_annotation() -> None:
    """全部工具调用拼装失败且无文本 → assistant content 不得是空列表。

    对应 P0 详规 81 行「已知边界」①的本体。修复前:
    ``content == []`` → 协议载荷 ``content: None``(拒收风险),
    且审计链上这一轮什么都没有。

    注入:回退 loop 侧注记 → 两条断言(content 非空 / 载荷非 null)都红。
    """
    loop = _make_loop(
        [
            # 第 1 轮:一个 JSON 截断的调用(拼装失败),没有文本
            [
                {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "call_bad",
                    "name": "echo",
                    "arguments_delta": '{"message": "hi"',
                },
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            # 第 2 轮:模型修正后正常收尾
            [
                {"type": "text_delta", "text": "改好了", "text_signature": None},
                {"type": "stop", "stop_reason": "stop"},
            ],
        ]
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    assistants = _assistants_of(result.messages)
    assert len(assistants) == 2

    # 审计链:第一轮的 assistant 必须留下可见痕迹(非空 content)
    first = assistants[0]
    assert first.content, "全部调用拼装失败且无文本的一轮,content 落成了空列表"
    texts = [b.text for b in first.content if isinstance(b, TextBlock)]
    assert any(ANNOTATION_MARK in t for t in texts), (
        f"空轮注记缺失,content={[b.type for b in first.content]}"
    )
    # 注记要说清"调用全部拼装失败"——审计价值就在这句话里
    assert any("拼装失败" in t for t in texts)

    # 协议侧:这一轮的 assistant 载荷 content 不得是 null
    payload = message_to_openai(first)
    assert payload["content"] is not None, (
        "空轮 assistant 的 OpenAI 载荷 content 为 null 且无 tool_calls,"
        "真实 API 可能直接拒收"
    )


@pytest.mark.asyncio
async def test_model_produces_nothing_at_all_leaves_visible_annotation() -> None:
    """模型一轮里既无文本也无工具调用 → 同样不得落成空 content。

    与缺陷①同一聚合条件("零文本+零有效调用")的孪生路径:
    "无调用"即收尾信号,所以这一轮**就是**整轮 turn 的唯一一轮——
    assistant 照样进历史,下一次 send 重发时面临完全相同的
    null-content 拒收风险。
    """
    loop = _make_loop(
        [
            # 唯一一轮:只有 stop,什么都没产出 → 按契约立即收尾
            [{"type": "stop", "stop_reason": "stop"}],
        ]
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    assistants = _assistants_of(result.messages)
    assert len(assistants) == 1

    first = assistants[0]
    assert first.content, "零产出轮的 assistant content 落成了空列表"
    texts = [b.text for b in first.content if isinstance(b, TextBlock)]
    assert any(ANNOTATION_MARK in t for t in texts)

    payload = message_to_openai(first)
    assert payload["content"] is not None


@pytest.mark.asyncio
async def test_annotation_does_not_disturb_existing_unparsed_user_note() -> None:
    """补注记不得破坏既有契约:unparsed 调用仍走 user 系统注记,工具仍不执行。

    这是修复的回归护栏:注记加在 assistant 消息上,G100 已钉住的
    "失败调用 → user 注记、不合成孤儿 tool_result"路径必须原样保留。
    """
    from sigma.agent.messages import ToolResultAgentMessage

    tool = EchoTool()
    loop = _make_loop(
        [
            [
                {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "call_bad",
                    "name": "echo",
                    "arguments_delta": '{"message": "hi"',
                },
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            [
                {"type": "text_delta", "text": "改好了", "text_signature": None},
                {"type": "stop", "stop_reason": "stop"},
            ],
        ]
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    assert tool.seen == []  # 失败调用不执行
    # 不合成孤儿 tool_result
    assert [m for m in result.messages if isinstance(m, ToolResultAgentMessage)] == []
    # user 系统注记仍在
    user_notes = [
        m.message
        for m in result.messages
        if isinstance(m, LlmMessageWrapper)
        and isinstance(m.message, UserMessage)
    ]
    assert any("无法解析" in str(n.content) for n in user_notes)
