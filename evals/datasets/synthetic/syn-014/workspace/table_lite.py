"""固定宽度文本表格渲染器。

产出可直接贴进终端或 README 的纯文本表格：

- 列宽 = max(表头宽度, 该列所有单元格宽度)，再被 ``max_widths`` 截上限；
- 单元格按 ``align`` 补空格：``left`` / ``right`` / ``center``；
  center 时总补白 = width - len(cell)，左边取总补白整除 2、
  **多出的那格放右边**；
- 超宽截断：``len(cell) > cap`` 时保留前 ``cap - 3`` 个字符再接 ``...``，
  总长恰好等于 cap；``cap < 4`` 抛 ``ValueError``；
- 行 = 单元格用 ``" | "`` 连接；表头下面一条分隔线，长度 =
  ``sum(widths) + 3 * (列数 - 1)``，全由 ``-`` 组成。
"""

from __future__ import annotations

_TRUNC_MARK = "..."


def truncate(cell: str, cap: int) -> str:
    """按上限截断：不超长原样返回；超长时前 cap-3 个字符 + ``...``。"""
    if cap < 4:
        raise ValueError(f"cap 至少要 4，收到 {cap}")
    if len(cell) <= cap:
        return cell
    return cell[:cap] + _TRUNC_MARK


def format_cell(cell: str, width: int, align: str = "left") -> str:
    """把 cell 补成正好 width 宽（cell 本身不截断，超宽原样返回）。"""
    if align == "right":
        return cell.rjust(width)
    if align == "center":
        total = width - len(cell)
        left = (total + 1) // 2
        return " " * left + cell + " " * (total - left)
    return cell.ljust(width)


def render_table(
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str] | None = None,
    max_widths: list[int] | None = None,
) -> str:
    """渲染表格；aligns 缺省全 left，max_widths 缺省不设上限（0 表示该列不限）。"""
    ncols = len(headers)
    if aligns is None:
        aligns = ["left"] * ncols
    if max_widths is None:
        max_widths = [0] * ncols
    widths: list[int] = []
    for c in range(ncols):
        cells = [len(row[c]) for row in rows]
        natural = max(cells) if cells else len(headers[c])
        widths.append(natural if max_widths[c] <= 0 else min(natural, max_widths[c]))

    def fmt(c: int, cell: str) -> str:
        if max_widths[c] > 0:
            cell = truncate(cell, max_widths[c])
        return format_cell(cell, widths[c], aligns[c])

    lines = [" | ".join(fmt(c, headers[c]) for c in range(ncols))]
    lines.append("-" * (sum(widths) + 3 * ncols))
    for row in rows:
        lines.append(" | ".join(fmt(c, row[c]) for c in range(ncols)))
    return "\n".join(lines)
