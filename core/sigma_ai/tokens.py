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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# 按 token 截断
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TruncatedText:
    """一次截断的结果。

    **为什么是 dataclass 而不是 BaseModel**：它不落盘、不过网，
    只是把一个返回值捆绑起来交给调用方立刻消费——判据与 ``TurnResult`` 一致。
    """

    text: str
    truncated: bool
    tokens: int
    original_tokens: int


#: 标记生成器：``(原文 token 数, 保留 token 数) -> 标记文本``。
#:
#: 做成**回调**而不是固定文案，是因为不同调用方的文案要带不同的东西
#: （AGENTS.md 要给文件路径、技能正文要给技能名），
#: 而"怎么在预算内塞下标记"这套算法是**与文案无关**的。
Marker = Callable[[int, int], str]


def truncate_to_tokens(
    text: str,
    max_tokens: int,
    *,
    markers: Sequence[Marker] = (),
    prefer_line_boundary: bool = True,
) -> TruncatedText:
    """把 ``text`` 截到 ``max_tokens`` 以内，并附一个**能塞进预算**的标记。

    **不变量：``estimate_text(结果) <= max_tokens``**，任何输入下都成立。
    （这条由 ``resources.py`` 那边的参数化用例扫多档上限钉住，本函数沿用同一算法。）

    做法是从"最长标记"往"最短标记"试，第一个装得下的就用：

    1. 用 ``kept_tokens=0`` 生成标记——那时丢失量最大、**文案也最长**，
       所以它的长度是这一形态的长度上界（拿它做预留一定够）；
    2. 正文容量 = 预算字符数 − 标记上界；
    3. 默认尽量切在行边界上（切在句中会产出一句"看起来完整、实际断了"的文本，
       那比明确少一段更危险）；
    4. 用真实的保留量重建标记，**再校验一次总长**。

    候选标记全部装不下时返回**空文本**，``truncated=True``——
    此时预算已经小到放不下一句说明，可见性由调用方的字段兜住。

    **为什么这段算法是共享的**

        它原先只存在于 ``sigma_session/resources.py``，2026-09-22 加技能系统时
        发现"按 token 截断并让丢弃可见"是**两个不相干的调用方都要的东西**。
        复制一份的代价不是多几行代码，而是**"毫秒"式的差异会悄悄出现**
        （一边截断、一边四舍五入），而症状是"同一个上限，两处结果不一样"。
    """
    original_tokens = estimate_text(text)
    if original_tokens <= max_tokens:
        return TruncatedText(text, False, original_tokens, original_tokens)

    budget_chars = int(max_tokens * CHARS_PER_TOKEN)
    for marker in markers:
        probe = marker(original_tokens, 0)
        cap = budget_chars - len(probe)
        if cap < 0:
            continue

        body = text[:cap]
        if prefer_line_boundary:
            newline = body.rfind("\n")
            if newline > cap // 2:
                body = body[: newline + 1]

        composed = body + marker(original_tokens, estimate_text(body))
        if len(composed) <= budget_chars:
            return TruncatedText(
                composed, True, estimate_text(composed), original_tokens
            )

    return TruncatedText("", True, 0, original_tokens)
