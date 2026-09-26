"""悬空 tool_call 修复：断点续跑的持久化基础（P4-批次5 Q3 拍板）。

**什么是悬空**
    树上有一条 assistant 消息带着 ``tool_call`` 块，但沿 head 分支往后
    找不到 ``tool_call_id`` 匹配的 tool_result 节点——中断（Ctrl-C /
    进程被杀 / 崩溃）发生在"LLM 已返回、结果尚未全部落盘"的窗口里。
    增量持久化把窗口缩到"单条结果落定"，但**窗口本身消不掉**：
    崩在批次执行中途，批内已完成但未落盘的结果就是悬空的。

**为什么必须修**
    直接续跑会把不完整的消息序列发给 provider——OpenAI 兼容协议要求
    assistant 的每个 ``tool_call`` 后面跟着对应 tool 结果，缺了就是 400。
    症状是"续跑必然失败"，而用户视角只是"上次没跑完"。

**为什么合成错误结果，而不是重跑**（Q3 拍板）
    重跑写工具可能双写（上次可能已经执行了一半）；"这个工具当时到底
    有没有执行成功"是一个**只有外部世界知道**的事实，harness 猜不得。
    所以合成一条 ``is_error=True`` 的结果如实说"中断了、结果未知"，
    重跑与否交模型决定——它是看到这条结果的人。

**幂等**
    修复节点落盘后，对应 id 就有了匹配的 tool_result，悬空消失；
    二次加载零新增。修复本身走 ``tree.append``（先落盘再改内存），
    修复中途再崩也不会留下"内存以为修了"的状态。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from sigma_agent.agent_messages import LlmMessageWrapper, ToolResultAgentMessage
from sigma_ai.messages import AssistantMessage, TextBlock, ToolCallBlock

if TYPE_CHECKING:
    from sigma_session.tree import SessionTree


def repair_dangling_tool_results(
    tree: SessionTree, *, clock: Callable[[], str]
) -> list[str]:
    """沿 head 分支补齐悬空的工具调用，返回**新落盘的修复节点 id**。

    无悬空时返回空列表、不写任何东西——所以它对健康会话是零开销、
    零副作用的，可以在每次接续会话时无条件调用。

    悬空的判定只看**本分支**（``tree.history()`` = head 路径）：
    其他分支的历史与本分支的续跑无关（``path_to`` 的 G48 语义）。
    """
    answered: set[str] = set()
    requested: list[tuple[str, str]] = []

    for message in tree.history():
        inner: object = message
        if isinstance(message, LlmMessageWrapper):
            inner = message.message
        if isinstance(inner, AssistantMessage):
            for block in inner.content:
                if isinstance(block, ToolCallBlock):
                    requested.append((block.id, block.name))
        elif isinstance(inner, ToolResultAgentMessage):
            answered.add(inner.tool_call_id)

    missing = [(call_id, name) for call_id, name in requested if call_id not in answered]

    repaired: list[str] = []
    for call_id, tool_name in missing:
        node_id = tree.append(
            ToolResultAgentMessage(
                tool_call_id=call_id,
                tool_name=tool_name,
                content=[
                    TextBlock(
                        text=(
                            "harness 在该工具执行完成之前被中断，结果未知"
                            "（可能已部分执行）。如仍需要，请重新调用该工具。"
                        )
                    )
                ],
                details={"repaired": True, "reason": "harness-interrupted"},
                is_error=True,
                timestamp=clock(),
            )
        )
        repaired.append(node_id)
    return repaired
