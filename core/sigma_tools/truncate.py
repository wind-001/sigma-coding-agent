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

    # 头尾要有重叠保护：文件很短但单行极长时，head+tail 可能超过总行数。
    head = lines[:head_lines]
    tail = lines[-tail_lines:] if total_lines > head_lines else []
    kept_lines = len(head) + len(tail)

    marker = (
        f"\n[... 输出被截断：共 {total_lines} 行 / {total_bytes} 字节，"
        f"此处只显示头 {len(head)} 行与尾 {len(tail)} 行。"
        "P1 版本不保存完整输出——**如需完整内容，请缩小命令的输出范围"
        "（例如加 | head、加 grep 过滤、或分批取）。**]\n"
    )

    body = "\n".join(head) + marker + "\n".join(tail)
    return Truncated(
        text=body,
        truncated=True,
        total_lines=total_lines,
        total_bytes=total_bytes,
        kept_lines=kept_lines,
    )
