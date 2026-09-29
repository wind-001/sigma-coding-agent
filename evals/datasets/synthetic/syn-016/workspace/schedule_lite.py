"""日程区间工具：合并、冲突检测、预约、空闲段。

约定：每个区间是 ``[start, end)``（左闭右开）的整数对（如当天第几分钟）；
首尾相接的两段（``[9,10)`` 与 ``[10,11)``）在**合并**时视为连续、要并成一段，
在**冲突判定**上不算冲突（10:00 结束的会与 10:00 开始的会可以排在同一天）。
"""

from __future__ import annotations


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把任意顺序给出的区间合并成互不重叠的列表（按 start 升序）。

    重叠**或首尾相接**的区间要并成一个；被别的区间完全包含的不能把外层截短。
    """
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start < merged[-1][1]:
            prev_start, _prev_end = merged[-1]
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    return merged


def find_conflict(
    bookings: list[tuple[int, int]], start: int, end: int
) -> tuple[int, int] | None:
    """返回 bookings 里第一个与 [start, end) 重叠的预约；没有则返回 None。

    重叠遵循左闭右开：首尾相接**不算**重叠。
    """
    for b_start, b_end in bookings:
        if start <= b_end and b_start <= end:
            return (b_start, b_end)
    return None


def book(
    bookings: list[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    """把 [start, end) 加进预约表；与现有预约冲突时抛 ValueError。

    返回新的预约列表（按 start 升序）；不改变传入的列表。
    """
    conflict = find_conflict(bookings, start, end)
    if conflict is not None:
        raise ValueError(f"时段 [{start}, {end}) 与现有预约 {conflict} 冲突")
    return sorted([*bookings, (start, end)])


def free_slots(
    bookings: list[tuple[int, int]], day_start: int, day_end: int
) -> list[tuple[int, int]]:
    """返回 [day_start, day_end) 里没被任何预约占用的空档（按序，含头尾空档）。

    窗口外的预约被忽略；最后一个预约之后到 day_end 的尾部空档也要输出。
    """
    free: list[tuple[int, int]] = []
    cursor = day_start
    for b_start, b_end in merge_intervals(bookings):
        if b_start >= day_end:
            break
        if b_start > cursor:
            free.append((cursor, b_start))
        cursor = max(cursor, b_end)
        if cursor >= day_end:
            return free
    return free
