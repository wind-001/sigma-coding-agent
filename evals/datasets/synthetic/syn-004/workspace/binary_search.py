"""有序列表上的二分查找与插入点。

- ``index_of``：找到返回下标，找不到返回 ``-1``；
- ``insertion_point``：语义与标准库 ``bisect.bisect_left`` 一致——
  返回第一个**不小于** target 的下标（列表里已有 target 时，
  返回它**第一个**出现的位置）。

两个函数都假定输入**升序**；空列表合法（查找得 -1，插入点为 0）。
"""

from __future__ import annotations


def index_of(items: list[int], target: int) -> int:
    lo, hi = 0, len(items) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if items[mid] == target:
            return mid
        if items[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def insertion_point(items: list[int], target: int) -> int:
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi) // 2
        if items[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo + 1
