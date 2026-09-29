"""最小堆：priority 数字越小越紧急；同 priority 按 seq（登记顺序）先出。

数组实现：下标 ``i`` 的左孩子在 ``2*i+1``，右孩子在 ``2*i+2``，父节点在 ``(i-1)//2``。
堆顶永远是「最该先出」的元素。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HeapEntry:
    """堆里的一条：priority 主序（小者先），seq 决定同优先级的先后。"""

    priority: int
    seq: int
    payload: str


class MinHeap:
    """手写的二叉最小堆，不借 ``heapq``。"""

    def __init__(self) -> None:
        self._entries: list[HeapEntry] = []

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _less(a: HeapEntry, b: HeapEntry) -> bool:
        """a 是否该排在 b 前面：先比 priority，再比 seq（先登记先出）。"""
        return a.priority < b.priority

    def push(self, priority: int, seq: int, payload: str) -> None:
        self._entries.append(HeapEntry(priority, seq, payload))
        self._sift_up(len(self._entries) - 1)

    def peek(self) -> HeapEntry:
        if not self._entries:
            raise IndexError("peek from empty heap")
        return self._entries[0]

    def pop(self) -> HeapEntry:
        if not self._entries:
            raise IndexError("pop from empty heap")
        top = self._entries[0]
        last = self._entries.pop()
        if self._entries:
            self._entries[0] = last
            self._sift_down(0)
        return top

    # ---- 内部：上浮 / 下沉 ----

    def _sift_up(self, index: int) -> None:
        while index > 0:
            parent = (index - 1) // 2
            if self._less(self._entries[index], self._entries[parent]):
                self._entries[index], self._entries[parent] = (
                    self._entries[parent],
                    self._entries[index],
                )
                index = parent
            else:
                break

    def _sift_down(self, index: int) -> None:
        size = len(self._entries)
        while True:
            smallest = index
            left = 2 * index + 1
            right = 2 * index + 2
            if left < size and self._less(self._entries[left], self._entries[smallest]):
                smallest = left
            if smallest == index:
                break
            self._entries[index], self._entries[smallest] = (
                self._entries[smallest],
                self._entries[index],
            )
            index = smallest
