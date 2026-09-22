"""会话压缩：把最旧的一段历史换成一个摘要，保留最近 N 轮原文。

**一句话模型**

    压缩不改变历史，只改变**发给模型的消息**。

为什么不是"把历史替换掉"（这是对 P2 详规 Q3 的有意偏离）
    Q3 的原话是「把最旧的一段消息**替换**成一条 `user` 消息」。P1 的历史是一个
    列表，"替换"就是重新赋值。但 P2-2 之后历史住在 ``SessionTree`` 里，而树是
    **只追加**的——"替换前缀"要么改写节点、要么重新挂父，两者都等于**改写历史**。

    改写历史的代价不是"实现麻烦"，而是**审计链断了**：
    ``store.py`` 的 docstring 已经写死——"会话历史是事实记录，改写它会让
    「这条消息当时是否存在过」变得不可考"。而这正是"影子 git + 会话树"
    这套设计存在的理由。

    所以压缩产出的是一个**派生视图**：``SessionContext`` 记住"从哪条消息开始保留"
    与那条摘要，组装时给出 ``[摘要, *保留的最近 N 轮]``。
    树里原文一个字节都不动。

**代价要说清楚**

    视图不落盘。所以重载会话后需要**重新压缩一次**（多花一次 LLM 调用）。
    把压缩结果持久化属于 P2-5（会话接续）的范围——那时才需要决定
    "摘要存哪儿、怎么和老消息共存"。**现在不做，是因为还没有那个需求，
    而不是忘了。**

压缩成本为什么要记进 ``details``
    P2 详规 Q3 / 风险 R2.4：一次压缩 = 一次 LLM 调用。这个成本不算进
    "每任务 token" 的话，**指标会系统性偏低**——长会话越省越显得划算，
    而省下来的正是没被记账的那次调用。``details`` 不进上下文，正适合放它。

摘要为什么降级成 ``user`` 而不是 ``system``
    架构 5.2 / D4：常驻区（系统提示词 + 工具 schema + 项目说明 + 技能索引）
    在整个会话里不得变化一个字节，否则 prompt cache 从变动点起全部失效。
    摘要每压缩一次就变一次——**它是动态内容**，只能进动态区。
    由门槛 G51 钉住。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sigma_ai import stamps
from sigma_agent.agent_messages import (
    AgentMessage,
    convert_to_llm,
    register_message_type,
)
from sigma_ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sigma_ai.base import BaseProvider, CancelToken, SamplingParams
    from sigma_ai.messages import LlmMessage

# ---------------------------------------------------------------------------
# 摘要消息
# ---------------------------------------------------------------------------


@register_message_type(role="compaction_summary")
class CompactionSummary(AgentMessage):
    """一条压缩摘要。**降级成 ``user``，不是 ``system``。**

    它是一个**应用层消息**（不是协议里的 role），所以注册在这里而不是
    ``sigma_agent``——正好演示 D3 说的"扩展层是运行期可变状态"：
    **不需要改核心包就能加消息类型**。

    ``compacted_messages`` / ``usage`` 都只用于审计与评测，不进上下文
    （``to_llm()`` 只渲染 ``summary``）。
    """

    role: Literal["compaction_summary"] = "compaction_summary"
    summary: str
    compacted_messages: int = 0
    #: 本次压缩那次 LLM 调用的用量。**不进上下文**，只给审计与评测（R2.4）。
    usage: Usage | None = None
    details: dict[str, Any] = {}

    def to_llm(self) -> LlmMessage | None:
        return UserMessage(content=self.render(), timestamp=self.timestamp)

    def render(self) -> str:
        """给模型看的正文。

        **开头必须说清"这不是新的请求"。** 摘要被渲染成 ``user`` 消息，
        而模型天然会把最后一条 user 消息当成当前任务——若摘要读起来像一段
        新指令，模型会去执行一段已经做完的事。这个前缀防的就是它。
        """
        return (
            "[以下是**更早会话**的压缩摘要，不是新的请求。"
            "请把它当作已经发生过的上下文。]\n\n"
            f"{self.summary}\n\n"
            "[摘要结束]"
        )


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


DEFAULT_TRIGGER_RATIO = 0.80
"""动态区用到预算的这个比例就触发压缩（P2 详规 Q3）。

**为什么不是 100%**：压缩本身要花一次 LLM 调用，而那次调用也要把
"要压缩的内容"发出去。等到上下文已经满了再压，可能连这一发都发不出去——
**触发点必须留出一次调用的余量。**
"""

DEFAULT_KEEP_RECENT_ROUNDS = 4
"""保留最近多少**轮**原文不压。

**为什么不能全压掉**：最近的对话决定当前决策。压掉它等于让 agent 失忆——
它会重新问用户已经说过的事，或者把刚改好的文件再改一遍。
"""


@dataclass(frozen=True)
class CompactionPolicy:
    """压缩的触发与保留策略。

    ``context_window_tokens`` **没有默认值**：它是"这个模型能装多少"，
    属于**调用方的知识**（模型选择在产品壳里），不是会话层能猜的。
    写一个默认值进去，等于让会话层假装知道一个它无从知道的事实——
    而猜错的后果（该压不压 → 请求被拒）离根因很远。

    判据与批次 8 的"工具层不自己去 ``Path.home()`` 猜密钥路径"一致：
    **谁决定策略，谁传参。**
    """

    context_window_tokens: int
    trigger_ratio: float = DEFAULT_TRIGGER_RATIO
    keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS

    def dynamic_budget_tokens(self, resident_tokens: int) -> int:
        """动态区可用预算 = 窗口 − 常驻区。

        常驻区每轮都要占着（D4），所以"留给历史的"是剩下的部分。
        负数直接夹到 0：那说明常驻区自己就超了窗口，
        此时**任何**压缩都救不了——由常驻区的预算闸（G59）去报那个错。
        """
        return max(0, self.context_window_tokens - resident_tokens)


def needs_compaction(
    dynamic_tokens: int, *, policy: CompactionPolicy, resident_tokens: int
) -> bool:
    """动态区是否已经该压缩了。

    ``dynamic_tokens`` 由调用方估算（``estimate_messages``）——
    本函数只做比较，不自己算 token：**估算口径只有一处**，
    散成两份就会在某一处悄悄漂移。
    """
    budget = policy.dynamic_budget_tokens(resident_tokens)
    if budget <= 0:
        return False
    return dynamic_tokens >= budget * policy.trigger_ratio


# ---------------------------------------------------------------------------
# 摘要提示词
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """你在压缩一段编程会话的历史。把下面的对话压成一份简短摘要。

摘要**必须**保留下面三类信息，缺一不可：

1. 用户的任务目标 —— 他到底要做什么，以及目标有没有中途变过。
2. 已经改过哪些文件 —— 具体到路径；把新建、修改、删除分开写。没有改动就写明"没有改动"。
3. 失败过什么 —— 试过但没成功的方向，以及失败的原因。
   这一条最容易漏，但**漏了 agent 会重复踩同一个坑**。

要求：
- 只输出摘要正文，不要复述这些指令，不要输出 JSON。
- 不要臆测：原文没提到的信息就不要写。
- 用简洁的中文，控制在 300 字以内。

下面是需要压缩的对话历史：
"""


def build_summary_prompt(history_text: str) -> str:
    """把待压缩的历史拼成一次 LLM 调用的输入。

    单独抽出来是为了让"提示词里到底有没有那三条要求"能被**直接断言**
    （门槛 G52）——藏在函数体里的字符串只能靠运行时抓，而那是脆的。
    """
    return f"{SUMMARY_PROMPT}\n{history_text}"


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------


def _is_user_message(message: AgentMessage) -> bool:
    """这条 agent 层消息会不会降级成 ``user``。

    **必须问 ``to_llm()`` 而不是看 ``role``**：agent 层的 ``role`` 是
    注册表用的（``"llm"`` / ``"tool_result"`` / …），与协议层的
    ``user`` / ``assistant`` 不是一回事。压缩摘要自己就降级成 user——
    但那不代表它是"用户说了话"。
    """
    llm = message.to_llm()
    return isinstance(llm, UserMessage)


def split_for_compaction(
    messages: Sequence[AgentMessage], *, keep_recent_rounds: int
) -> tuple[list[AgentMessage], list[AgentMessage]]:
    """按"轮"切分，返回 ``(要压缩的, 要保留的)``。

    **一轮的边界是一条 user 消息。** 之所以不用"一条 assistant 消息"：
    一轮里模型可能调用多次工具（assistant → tool_result → assistant → …），
    按 assistant 切会把同一轮切成好几段，于是"保留最近 N 轮"实际只保留了
    N 次模型调用——**与意图不符，而且症状是"保留得比预期少"**。

    保留数不足 / 没有可切的边界时返回 ``( [], 全部 )``——
    即"什么都不压"。压缩不该在无从下手时硬压，那只会丢信息。
    """
    if keep_recent_rounds <= 0:
        return [], list(messages)

    boundaries = [
        index for index, message in enumerate(messages) if _is_user_message(message)
    ]
    if len(boundaries) <= keep_recent_rounds:
        return [], list(messages)

    cut = boundaries[-keep_recent_rounds]
    if cut <= 0:
        return [], list(messages)
    return list(messages[:cut]), list(messages[cut:])


# ---------------------------------------------------------------------------
# 渲染待压缩的历史
# ---------------------------------------------------------------------------


def _flatten(message: LlmMessage) -> str:
    """把一条 LLM 层消息压成一行文本，供摘要调用阅读。

    只取文本与工具调用/结果的要点：摘要要的是"发生了什么"，
    不是原文复现。``thinking`` 内容**刻意丢掉**——它是模型的草稿，
    对"任务目标 / 改了什么 / 失败过什么"三类信息没有贡献，
    带上只会挤占待压缩内容的篇幅。
    """
    if isinstance(message, SystemMessage):
        return message.content if isinstance(message.content, str) else ""

    if isinstance(message, ToolResultMessage):
        parts = [block.text for block in message.content if isinstance(block, TextBlock)]
        head = f"[工具 {message.tool_name} 结果"
        head += "（失败）]" if message.is_error else "]"
        return f"{head} {' '.join(parts)}"

    if isinstance(message, AssistantMessage):
        texts: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif hasattr(block, "name"):
                texts.append(f"[调用工具 {block.name}]")
        return " ".join(texts)

    if isinstance(message, UserMessage):
        return message.content if isinstance(message.content, str) else ""

    return ""


def render_history(messages: Sequence[AgentMessage]) -> str:
    """把一段历史渲染成摘要调用的输入文本。

    走 ``convert_to_llm`` 而不是直接读 agent 层字段：那是 agent → LLM 的
    **唯一通道**（架构 4.0），绕过它意味着"存了但模型看不见"的消息
    在这里突然变得可见——两处口径就会不一致。
    """
    lines: list[str] = []
    for llm_message in convert_to_llm(list(messages)):
        text = _flatten(llm_message).strip()
        if text:
            lines.append(f"[{llm_message.role}] {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionOutcome:
    """一次压缩的结果。

    **为什么是 dataclass 而不是 BaseModel**：它不落盘、不过网，
    只是一次调用的返回值——判据与 ``TurnResult`` / ``LoadResult`` 一致。
    """

    summary: CompactionSummary
    compacted_messages: int
    kept_messages: int
    usage: Usage | None = None


async def summarize(
    messages: Sequence[AgentMessage],
    *,
    provider: BaseProvider,
    model: str,
    signal: CancelToken,
    sampling: SamplingParams | None = None,
    clock: Callable[[], str] | None = None,
) -> tuple[str, Usage | None]:
    """调一次模型，把 ``messages`` 压成摘要文本。返回 ``(摘要, 用量)``。

    用 ``provider.stream`` 而不是另开一个"非流式"接口：``sigma_ai`` 只有
    一个 ``stream``（架构 4.1），为摘要加第二条通道等于让 provider 契约
    长出分叉——而每多一条通道，就多一处"两个实现者只有一个支持"的风险。

    ``tools=[]``：摘要调用不需要工具。传工具 schema 反而会诱使模型去调工具，
    而这一次调用的产物**必须是文本**（否则压缩拿不到摘要，整条链断掉）。
    """
    from sigma_ai.events import ErrorEvent, StopEvent, TextDelta, UsageEvent

    prompt = build_summary_prompt(render_history(messages))
    stamp = clock() if clock is not None else stamps.now()
    # 显式标成 ``list[LlmMessage]``：``list`` 是不变的，
    # 写 ``list[UserMessage]`` 会被 mypy 判成与 ``stream`` 的参数不兼容。
    request: list[LlmMessage] = [UserMessage(content=prompt, timestamp=stamp)]

    parts: list[str] = []
    usage: Usage | None = None
    errors: list[str] = []

    async for event in provider.stream(
        request, [], model=model, signal=signal, sampling=sampling
    ):
        if isinstance(event, TextDelta):
            parts.append(event.text)
        elif isinstance(event, UsageEvent):
            usage = event.usage
        elif isinstance(event, ErrorEvent):
            errors.append(f"{event.error.code}: {event.error.message}")
        elif isinstance(event, StopEvent):
            continue

    text = "".join(parts).strip()
    if not text:
        # 空摘要比不压缩更坏：它会让模型以为"之前什么都没发生"。
        # 宁可报错让调用方决定（重试 / 跳过压缩），也不要静默塞一条空的。
        detail = f"（provider 报告：{'; '.join(errors)}）" if errors else ""
        raise EmptySummary(f"压缩调用没有产出任何文本{detail}")
    return text, usage


class EmptySummary(RuntimeError):
    """压缩调用产出了空摘要。见 :func:`summarize` 里的说明。"""


async def compact_history(
    messages: Sequence[AgentMessage],
    *,
    policy: CompactionPolicy,
    provider: BaseProvider,
    model: str,
    signal: CancelToken,
    sampling: SamplingParams | None = None,
    clock: Callable[[], str] | None = None,
) -> CompactionOutcome | None:
    """按策略压缩。**没有可压的段时返回 ``None``**（不是空结果）。

    名字带 ``_history`` 而不是叫 ``compact``：``SessionContext`` 上有一个
    ``compact()`` 方法（它负责把结果接到视图上），两个同名会让调用点读不出
    到底是"压缩一次"还是"压缩并应用"。

    返回 ``None`` 与返回一个"压了 0 条"的结果是两回事：
    前者是"这次不需要压"，后者是"压了但没效果"——而后者如果真的出现，
    说明切分逻辑有问题。**用 ``None`` 把这两种情况分开。**
    """
    to_compact, kept = split_for_compaction(
        messages, keep_recent_rounds=policy.keep_recent_rounds
    )
    if not to_compact:
        return None

    text, usage = await summarize(
        to_compact,
        provider=provider,
        model=model,
        signal=signal,
        sampling=sampling,
        clock=clock,
    )
    return CompactionOutcome(
        summary=CompactionSummary(
            timestamp=clock() if clock is not None else stamps.now(),
            summary=text,
            compacted_messages=len(to_compact),
            usage=usage,
            details={
                # R2.4：压缩的成本必须能被评测拿到，否则"每任务 token"偏低。
                "compaction_usage": usage.model_dump() if usage else None,
                "compacted_messages": len(to_compact),
                "kept_messages": len(kept),
            },
        ),
        compacted_messages=len(to_compact),
        kept_messages=len(kept),
        usage=usage,
    )
