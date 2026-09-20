"""token 估算：保守粗估，明确不是账本。

为什么不做精确 tokenizer
    不同 provider 的 tokenizer 不同（DeepSeek / GLM / 通义各有各的），
    自实现一个既不准又要维护，而且**永远追不上厂商的更新**。
    真实用量以 provider 返回的 ``Usage`` 为准。

本模块的定位（详规 4.1 节）
    **只用于预算预警**——接近上限时提醒一句。
    **绝不进评测报告**：报告里的 token 数字必须来自 provider 的 ``usage``。
    估算数进报告等于造假。

算法
    按字符数粗估。中文约 1.5 字符/token，英文约 4 字符/token，
    混排取 **3 字符/token** 作为居中偏保守的值。
    这个系数应当**用真实 ``Usage`` 持续校准**——见 ``calibrate_factor()``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sigma_ai.messages import (
    AssistantMessage,
    ContentBlock,
    SystemMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from sigma_ai.messages import LlmMessage

# 每 token 对应的字符数。偏保守（宁可高估），因为它只用于预警。
CHARS_PER_TOKEN: float = 3.0

# 每条消息的固定开销（role、分隔符等），经验值。
PER_MESSAGE_OVERHEAD: int = 4


def estimate_text(text: str) -> int:
    """估算一段文本的 token 数。"""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def _estimate_blocks(blocks: list[ContentBlock] | list[TextBlock]) -> int:
    total = 0
    for block in blocks:
        if isinstance(block, TextBlock):
            total += estimate_text(block.text)
        elif hasattr(block, "thinking"):
            total += estimate_text(block.thinking)
        elif hasattr(block, "arguments"):
            # 工具调用的参数按序列化后的长度估
            total += estimate_text(str(block.arguments))
        elif hasattr(block, "data"):
            # 图片按固定成本估，不按 base64 长度（那会严重高估）
            total += 256
    return total


def estimate_messages(messages: list[LlmMessage]) -> int:
    """估算一组消息的 token 数。

    **这是预警值，不是账本。** 见模块 docstring。

    对 ``ToolResultMessage.details`` 明确不计入——它不进上下文
    （架构方案 4.2 节），计进去会虚高。
    """
    total = 0
    for message in messages:
        total += PER_MESSAGE_OVERHEAD

        if isinstance(message, SystemMessage):
            if isinstance(message.content, str):
                total += estimate_text(message.content)
            else:
                total += _estimate_blocks(message.content)
            # sections 也是进上下文的提示词内容
            if message.sections:
                for key, value in message.sections.items():
                    total += estimate_text(key)
                    if value is not None:
                        total += estimate_text(value)

        elif isinstance(message, UserMessage):
            if isinstance(message.content, str):
                total += estimate_text(message.content)
            else:
                total += _estimate_blocks(message.content)

        elif isinstance(message, AssistantMessage):
            total += _estimate_blocks(message.content)

        elif isinstance(message, ToolResultMessage):
            # 注意：details 不计入——它不进上下文
            total += _estimate_blocks(message.content)

    return total


def calibrate_factor(estimated: int, actual_prompt_tokens: int) -> float:
    """用真实用量反推系数，供人工调整 ``CHARS_PER_TOKEN``。

    返回值是"建议的字符/token"系数。**不要自动应用**——
    让调用方看到偏差后再决定，避免一次异常样本把系数带偏。
    """
    if estimated <= 0 or actual_prompt_tokens <= 0:
        return CHARS_PER_TOKEN
    ratio = actual_prompt_tokens / estimated
    return CHARS_PER_TOKEN / ratio if ratio > 0 else CHARS_PER_TOKEN
