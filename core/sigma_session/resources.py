"""项目资源：``AGENTS.md`` 的读取与硬截断。

分层位置
    本模块属于 ``sigma_session``（L3），只做**纯文件读取 + 截断**。
    **它不决定从哪读**——路径由调用方传入（架构 §4 的落点表就这么定的）。

    "从哪读"是产品壳（``sigma``）的策略：工作区根？向上找？
    多个文件谁覆盖谁？这些都需要**优先级规则与停止条件**，
    而本阶段没有已验证的需求。**先不做，比做一个半成品再推翻便宜。**

为什么截断是硬的、且必须写进正文
    ``AGENTS.md`` 落在**常驻区**（D4）——它每多一个字节，每轮请求都要重付一次。
    所以它必须有上限。

    但截断有一个容易忽略的后果：**模型不知道自己看到的不是全部**。
    它会以为"项目就这些约定"，于是不会去补看。
    这与批次 8 的「丢弃必须可见」是同一条纪律——
    **被硬规则砍掉的东西，计数要写回模型能看到的正文里**，
    否则模型不会自我纠正。所以截断标记拼在**正文**里，不是放在 details。

为什么上限的默认值是 800 而不是架构表里的 1,500
    P2-3 实测（2026-09-21）：常驻区其余部分实测 2 015 token
    （系统提示词 286 + 工具 schema 1 729，两个联网工具全开）。
    按 D4 的 3 500 上限，AGENTS.md 最多只能占 1 485——
    1 500 会**刚好超 15 token**。而 P4 还要加技能索引（≤600）。

    所以取 800：留出约 685 token 给技能索引，两者之和仍在上限内。
    **收紧的是 AGENTS.md 的上限，不是放宽 D4**——
    D4 是 prompt cache 的经济性主张，放宽它等于把主张改掉以迁就实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sigma_ai.tokens import CHARS_PER_TOKEN, estimate_text

AGENTS_MD_FILENAME = "AGENTS.md"
"""约定的项目说明文件名。**只用于给调用方一个默认值**，
本模块自己不拿它去搜路径（见模块 docstring）。"""

DEFAULT_MAX_TOKENS = 800
"""``AGENTS.md`` 进常驻区的 token 上限。理由见模块 docstring。"""


@dataclass(frozen=True)
class ProjectInstructions:
    """读到的项目说明。

    ``text`` 已经**包含**截断标记（若有截断）——调用方拿到它直接拼进系统提示词即可，
    不需要再自己判断是否被截断。**把判断留给调用方是一种浪费**：
    每个调用点都要重新想一遍"要不要提示模型"，而那正是最容易漏的一步。

    ``error`` 非空表示读到了文件但读不出来（编码问题 / 权限 / 是目录）。
    **不抛异常**：一份读不了的项目说明不该让整个会话起不来。
    但也不能静默——所以它作为数据返回，由调用方决定要不要展示。
    """

    text: str = ""
    source: Path | None = None
    truncated: bool = False
    tokens: int = 0
    original_tokens: int = 0
    error: str = ""

    @property
    def found(self) -> bool:
        """文件存在且读出了内容（或读出了空文件）。"""
        return self.source is not None and not self.error

    @property
    def loaded(self) -> bool:
        """有实际内容可以注入。"""
        return bool(self.text)


def _long_marker(path: Path, original_tokens: int, kept_tokens: int) -> str:
    """详细标记：说清丢了多少、怎么拿全文。

    **它必须给出行动指引**，不能只说"被截断了"——只有"被截断"三个字，
    模型会不知所措（既不知道丢了多少，也不知道怎么拿到）。
    这与 ``truncate.py`` 的文案要求一致：截断的**目的**是让模型换一种做法，
    而换法必须写在文案里。
    """
    dropped = original_tokens - kept_tokens
    return (
        f"\n\n[项目说明已截断：原文约 {original_tokens} token，"
        f"此处只保留前 {kept_tokens} token，丢弃约 {dropped} token。"
        f"完整内容请用 read 工具读取 {path}。以上约定可能不完整。]"
    )


def _short_marker(path: Path, original_tokens: int, kept_tokens: int) -> str:
    """短标记：预算塞不下详细标记时的退路。

    **它存在的理由是一个真实的缺陷**（2026-09-21 由参数化用例抓出来）：
    详细标记本身约 180 字符（≈60 token）。当调用方把上限配得很小
    （比如 50）时，"先按上限切正文、再拼标记"会让**标记自己**突破上限——
    于是"截断到 50"产出 61 token 的结果。

    两个纪律在这里冲突：**不得超过预算**（D4，硬约束）与
    **丢弃必须可见**（软目标，但很重要）。
    处置是**两个都保**——把标记压缩到能塞进预算，而不是二选一。
    "什么都不返回且不说为什么"是最坏的组合：模型既看不到约定，
    也不知道有东西被丢了。

    **参数与** :func:`_long_marker` **保持同形**（虽然这里用不到后两个）：
    ``_compose_truncated`` 要在两者之间循环挑选，
    签名不一致就没法用同一段代码调它们——而"两个函数长得不一样"
    正是那种会在重构时被漏掉的差异。
    """
    del original_tokens, kept_tokens  # 短标记不报数字，只保证可见
    return f"\n[...AGENTS.md 已截断，全文见 {path.name} ...]"


def _compose_truncated(
    raw: str, path: Path, budget_chars: int, original_tokens: int
) -> str:
    """把 ``raw`` 截到 ``budget_chars`` **字符**之内，并带上一个能塞进去的标记。

    **保证 ``len(结果) <= budget_chars``**，任何输入下都成立——
    这条不变量由 ``test_budget_is_never_exceeded`` 参数化扫多档上限来钉。

    做法是从"最长标记"往"最短标记"试，第一个能装下的就用：

    1. 用 ``kept_tokens=0`` 生成标记——那时 ``dropped`` 最大、**文案最长**，
       所以它的长度是这一形态的长度上界（拿它做预留一定够）；
    2. 正文容量 = 预算 − 标记上界；
    3. 尽量切在行边界（切在句中会产出"看起来完整、实际断了"的约定）；
    4. 用真实 ``kept_tokens`` 重新生成标记，**再校验一次总长**。

    两种标记都装不下时返回空串：此时预算已经小到放不下一句说明，
    由调用方的 ``truncated`` / ``original_tokens`` 字段兜住可见性。
    """
    for maker in (_long_marker, _short_marker):
        probe = maker(path, original_tokens, 0)  # kept=0 → dropped 最大 → 最长形态
        cap = budget_chars - len(probe)
        if cap < 0:
            continue

        body = raw[:cap]
        newline = body.rfind("\n")
        if newline > cap // 2:
            body = body[: newline + 1]

        marker = maker(path, original_tokens, estimate_text(body))
        text = body + marker
        if len(text) <= budget_chars:
            return text

    return ""


def load_project_instructions(
    path: Path | None, *, max_tokens: int = DEFAULT_MAX_TOKENS
) -> ProjectInstructions:
    """读取项目说明并硬截断到 ``max_tokens``。

    四种输入都有明确结果，**一条都不抛异常**：

    | 情况 | 结果 |
    | --- | --- |
    | ``path`` 为 None | 空结果（调用方明确表示"不要项目说明"）|
    | 文件不存在 | 空结果，``error`` 为空——**没有那就是没有，不是错误** |
    | 读得出来 | 文本（可能带截断标记）|
    | 读不出来（编码/权限/是目录）| 空文本 + ``error`` 说明原因 |

    "文件不存在不是错误"这条很重要：绝大多数项目没有 ``AGENTS.md``，
    若它报错，每个入口都要先 ``if exists()``——而那是最容易漏的约定。
    """
    if path is None:
        return ProjectInstructions()

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProjectInstructions()
    except (OSError, UnicodeDecodeError) as exc:
        # 不抛：读不了项目说明不该让会话起不来。但必须留下原因。
        return ProjectInstructions(
            source=path, error=f"{type(exc).__name__}: {exc}"
        )

    original_tokens = estimate_text(raw)
    if original_tokens <= max_tokens:
        return ProjectInstructions(
            text=raw,
            source=path,
            tokens=original_tokens,
            original_tokens=original_tokens,
        )

    # 预算换算成字符：估算器是 chars / CHARS_PER_TOKEN。
    text = _compose_truncated(
        raw, path, int(max_tokens * CHARS_PER_TOKEN), original_tokens
    )
    return ProjectInstructions(
        text=text,
        source=path,
        truncated=True,
        tokens=estimate_text(text),
        original_tokens=original_tokens,
    )
