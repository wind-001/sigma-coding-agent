"""LLM 层消息 → OpenAI 请求体的转换。

为什么单独成文件
    转换逻辑是本层**最容易出错**的地方（内容块、tool_calls、
    `tool` 角色的特例），所以它值得一个能被单测直接打的落点——
    不必启动 HTTP 就能断言转换结果。

本模块是纯函数，不碰网络、不持有状态。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sigma_ai.messages import (
    AssistantMessage,
    ImageBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from sigma_ai.messages import LlmMessage


# ---------------------------------------------------------------------------
# 消息转换：LLM 层 → OpenAI 请求体
# ---------------------------------------------------------------------------


def _blocks_to_openai_content(
    content: str | list[Any],
) -> str | list[dict[str, Any]]:
    """把内容块列表转成 OpenAI 的 ``content`` 形状。

    纯文本且无签名时降级成裸字符串——少一层嵌套，也少几个 token。

    ``ToolCallBlock`` 为什么是 ``continue`` 而不是报错
        工具调用在 OpenAI 协议里**不放在 ``content`` 数组里**，
        它是与之并列的 ``tool_calls`` 字段（由 ``message_to_openai`` 组装）。
        所以这里必须跳过它。

        这个分支曾是缺失的，表现为：一条既有文本又有工具调用的 assistant
        消息会直接抛 ``TypeError``——而那正是**最常见的模型输出形态**
        （"我先看一下" + read 调用）。单测 ``test_assistant_text_and_tool_call_coexist``
        逮住了它。
    """
    if isinstance(content, str):
        return content

    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{block.mime_type};base64,{block.data}"},
                }
            )
        elif isinstance(block, ToolCallBlock):
            # 走 tool_calls 字段，不进 content。见 docstring。
            continue
        elif isinstance(block, ThinkingBlock):
            # 思考块不回传给 OpenAI 兼容端点——协议里没有这个位置。
            # 但它必须留在本地消息里（否则丢 signature），所以这里只是不发它。
            continue
        else:
            raise TypeError(f"无法转换的内容块类型：{type(block).__name__}")

    # 只剩一个文本块时降级成字符串
    if len(parts) == 1 and parts[0].get("type") == "text":
        return str(parts[0]["text"])
    return parts


def message_to_openai(message: LlmMessage) -> dict[str, Any]:
    """把单个 LLM 消息转成 OpenAI 请求体里的 dict。

    单独抽成函数是为了让单测能直接断言转换结果，
    不必启动 HTTP——转换逻辑是本层最容易出错的地方。
    """
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _blocks_to_openai_content(message.content)}

    if isinstance(message, UserMessage):
        return {"role": "user", "content": _blocks_to_openai_content(message.content)}

    if isinstance(message, AssistantMessage):
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": _blocks_to_openai_content(message.content) or None,
        }
        tool_calls = [
            block for block in message.content if isinstance(block, ToolCallBlock)
        ]
        if tool_calls:
            payload["tool_calls"] = [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.arguments, ensure_ascii=False),
                    },
                }
                for block in tool_calls
            ]
        return payload

    if isinstance(message, ToolResultMessage):
        # 工具结果在 OpenAI 协议里是独立的 role="tool" 消息，
        # 靠 tool_call_id 关联——这正是"tool_result 是独立角色"（4.0.1 节）的收益。
        text = _blocks_to_openai_content(message.content)
        if not isinstance(text, str):
            # 协议要求 tool 消息的 content 是字符串；要发图片得另想办法。
            # 这里不静默降级成 str(list)——那会产生没人能读的字符串。
            raise TypeError("工具结果的 content 暂不支持多模态内容块")
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": text,
        }

    raise ValueError(f"未知的 LLM 消息类型：{type(message).__name__}")


