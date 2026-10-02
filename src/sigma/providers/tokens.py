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

from sigma.providers.messages import (
    AssistantMessage,
    ContentBlock,
    SystemMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from sigma.providers.messages import LlmMessage

# 每 token 对应的字符数。偏保守（宁可高估），因为它只用于预警。
CHARS_PER_TOKEN: float = 3.0

# 每条消息的固定开销（role、分隔符等），经验值。
PER_MESSAGE_OVERHEAD: int = 4

# 截断时为"把正文砍短几行以腾出标记空间"设的上限。
# 理由：probe（marker(kept=0)）只是长度**上界**的近似，真构造时标记长度会变，
# 余量常只剩个位数字符 ⇒ 整档标记被跳过、退到短标记（详见 truncate_to_tokens）。
# 砍 12 行约 400–500 字符，足够吸收标记的长度抖动；再多就白丢正文了。
MAX_SHRINK_LINES: int = 12

# 正文回退到行边界后的最小长度（字符）。低于此值不再砍——
# 否则"为了保住详细标记"会把正文砍到几乎不剩，得不偿失。
MIN_BODY_CHARS: int = 32


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

        它原先只存在于 ``sigma.sessions/resources.py``，2026-09-22 加技能系统时
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
            # 回退到行边界，但**不设比例下限**（2026-10-02 改）。
            #
            # 原实现是 `if newline > cap // 2:`。改它的理由**不是**"实测在这个
            # 比例下会落半行"——我扫过 pathlen 60→4000、预算 200→900，
            # `cap // 2` 在所有实测点都成立，那条路是**不可观测的**。
            # 真正的理由是**它是个没有依据的魔数**：条件成立与否取决于
            # "这一行恰好有多长"，而调用方无从预知。与其留一个
            # "碰巧都对"的阈值，不如把语义写实——要么回退，要么不动。
            #
            # MIN_BODY_CHARS 这道下限是**要的**：cap 极小时回退会砍到
            # body 几乎不剩，此时"少留一点"与"留一句断话"不可兼得，
            # 优先保正文（"丢弃必须可见"由标记字段兜住）。
            #
            # ⚠ 回退后正文变短，标记里的"保留前 N token"必须用**回退后的
            # 真实保留量**重算（下面那行已经是），否则正文与标记自相矛盾
            # （说留 743 实际只留 400）。
            newline = body.rfind("\n")
            if newline != -1 and newline >= MIN_BODY_CHARS:
                body = body[: newline + 1]

        composed = body + marker(original_tokens, estimate_text(body))
        if len(composed) <= budget_chars:
            return TruncatedText(
                composed, True, estimate_text(composed), original_tokens
            )

        # ── 装不下时的第二道（2026-10-02 加）──────────────────────────
        # 上面用 ``probe = marker(original, 0)`` 预留，那**是这一形态的
        # 长度上界**吗？**不是。** probe 的 kept=0 ⇒ 丢失量最大 ⇒ 文案最长，
        # 看起来最安全；但真正构造 marker 时用的是 estimate_text(body)，
        # 正文回退到行边界后这个值会变化，标记长度也跟着变。
        # 实测余量常只剩 1–27 字符（预算 2400、标记 140–266），
        # 路径一长或回退幅度一变就 ``len(composed) > budget`` ⇒
        # **这一档标记被整条跳过**，退到短标记。
        #
        # 症状是「同一份 AGENTS.md，有时给详细标记、有时只给一句
        # `全文见 …`，取决于文件路径多长」——行为随环境漂移，
        # 而详细标记里才有"丢弃约 N token"和 read 指引。
        #
        # 修法不是放宽预算（那是改 D4 主张），而是**收紧 cap 再试一次**：
        # 多砍掉几行正文换回标记档，代价是几条约定，
        # 收益是"丢弃可见 + 行动指引"这条纪律不丢。
        # 逐行收缩（每次一行）而不是猜一个魔数——猜的偏移量会再次失效。
        for _ in range(MAX_SHRINK_LINES):
            newline = body.rfind("\n", 0, len(body) - 1)
            if newline < MIN_BODY_CHARS:
                break
            body = body[: newline + 1]
            composed = body + marker(original_tokens, estimate_text(body))
            if len(composed) <= budget_chars:
                return TruncatedText(
                    composed, True, estimate_text(composed), original_tokens
                )

    return TruncatedText("", True, 0, original_tokens)
