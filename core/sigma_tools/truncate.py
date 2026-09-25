"""工具输出截断（Q2 已拍板提前到 P1）。

P1 只做**截断**，不做落盘、不做分页。

三元策略在 P1 降为二元
    架构方案 5.3 节写的是三元，中间档（8 KB–256 KB）要把完整输出落盘到
    会话临时目录。**P1 没有落盘能力**，所以中间档与高档合并成同一处理——
    只少了"完整输出的文件路径"这一项。

    这个差异有**可观测的后果**：模型被截断后拿不到完整内容。
    对文件类输出它还能用 ``read`` 的行范围补救；对 ``bash`` 的输出**就是真的丢了**。

    所以截断文案必须写清这一点，**并给出行动指引**——
    "请缩小命令的输出范围"而不是"输出已被截断"。
    前者让模型改变做法，后者只会让它重试同一个命令。
    这条文案本身值得一个单测（门槛 G29）。
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_BYTES = 8 * 1024

HEAD_LINES = 40

TAIL_LINES = 40


@dataclass
class Truncated:
    """截断结果。

    带统计字段是因为它们要进 ``ToolResult.details``（给评测用）：
    "截断发生率"本身就是一个值得报告的指标（架构 5.3 节末）。
    """

    text: str
    truncated: bool
    total_lines: int
    total_bytes: int
    kept_lines: int


def truncate_output(
    text: str,
    *,
    max_bytes: int = MAX_BYTES,
    head_lines: int = HEAD_LINES,
    tail_lines: int = TAIL_LINES,
) -> Truncated:
    """按**字节上限**截断，超限时保留头尾各若干行。

    为什么阈值按字节而不是按行：
        一条 ``base64`` 输出可以只有 3 行却有 10 MB。
        按行限制拦不住它，而爆上下文的是 token 不是行数。

    为什么保留头**和**尾：
        命令的报错通常在**尾部**，而命令本身的信息在**头部**。
        只保留头部会丢掉最关键的纠错信息（详规 3.6）。

    截断标记里必须包含**总行数与总字节数**——模型据此判断
    "是差一点还是差很多"，决定要不要换个方式取数据。
    """
    total_bytes = len(text.encode("utf-8"))
    lines = text.splitlines()
    total_lines = len(lines)

    if total_bytes <= max_bytes:
        return Truncated(
            text=text,
            truncated=False,
            total_lines=total_lines,
            total_bytes=total_bytes,
            kept_lines=total_lines,
        )

    marker = (
        f"\n[... 输出被截断：共 {total_lines} 行 / {total_bytes} 字节，"
        "此处只保留头尾各若干行。"
        "P1 版本不保存完整输出——**如需完整内容，请缩小命令的输出范围"
        "（例如加 | head、加 grep 过滤、或分批取）。**]\n"
    )

    # 预算是**硬上限**，不是"入口看一眼就不管"（2026-09-24 review 修复）：
    # 一条 base64 输出可以只有 3 行却有 10 MB——总行数 ≤ head_lines 时
    # "取头 40 行"就是取全部，截断完全失效。所以单行与总量都要卡：
    # 单行先截断（超长行本身就要拦），再按剩余字节预算从两头**贪心装填**，
    # 保证 body + marker ≤ max_bytes。
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    tail_source = (
        lines[max(head_lines, total_lines - tail_lines) :]
        if total_lines > head_lines
        else []
    )
    # 只有头尾**都要留**时才对半分：没有尾段（总行数 ≤ head_lines）时把全部
    # 预算给头段。否则一半预算被白白闲置——明明装得下的数据被砍掉，
    # 而这在"行数少但行很长"的输出里恰恰是常态。
    head_budget = budget // 2 if tail_source else budget
    head = _fill(lines[:head_lines], head_budget)
    tail_budget = budget - _utf8_len("\n".join(head))
    tail = _fill(list(reversed(tail_source)), tail_budget)
    tail.reverse()
    kept_lines = len(head) + len(tail)

    body = "\n".join(head) + marker + "\n".join(tail)
    return Truncated(
        text=body,
        truncated=True,
        total_lines=total_lines,
        total_bytes=total_bytes,
        kept_lines=kept_lines,
    )


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_bytes(text: str, budget: int) -> str:
    """按 UTF-8 字节预算截断，**不切断多字节字符**。"""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    return raw[:budget].decode("utf-8", errors="ignore")


def _fill(lines: list[str], budget: int) -> list[str]:
    """按字节预算从``lines``头部贪心装填，单行超限先截断。

    被截断的行带行内标记——否则模型会把"半行"当成完整的一行，
    基于残缺数据继续推理。标记本身也计进预算（预算不够就纯截断）。
    """
    out: list[str] = []
    remaining = budget
    suffix = " [...本行已截断]"
    suffix_len = _utf8_len(suffix)
    for line in lines:
        if remaining <= 0:
            break
        if _utf8_len(line) > remaining:
            # 给行内标记**预留**字节：标记不出现的"截断"等于悄悄丢数据——
            # 模型会把半行当成完整的一行继续推理。
            piece_budget = max(0, remaining - suffix_len)
            out.append(_cut_bytes(line, piece_budget) + suffix)
            break  # 预算已尽，后面的行不再取
        out.append(line)
        remaining -= _utf8_len(line) + 1  # +1 是 join 时的换行
    return out
