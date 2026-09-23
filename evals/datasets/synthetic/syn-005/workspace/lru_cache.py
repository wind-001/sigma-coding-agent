"""固定容量的 LRU 缓存。

``get`` 与 ``put`` 都算一次"使用"；容量满时淘汰**最久未被使用**的那一条。
写操作覆盖同键的值时，也算一次新的使用（它显然刚被碰过）。
"""

from __future__ import annotations


class LRUCache:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity 必须为正数，收到 {capacity}")
        self._capacity = capacity
        self._data: dict[str, int] = {}

    def get(self, key: str) -> int | None:
        """取值；命中也算一次使用。未命中返回 ``None``。"""
        return self._data.get(key)

    def put(self, key: str, value: int) -> None:
        """写入；容量满时先淘汰最久未使用的键。"""
        if key in self._data:
            self._data[key] = value
            return
        if len(self._data) >= self._capacity:
            self._data.pop(next(iter(self._data)))
        self._data[key] = value

    def __len__(self) -> int:
        return len(self._data)

    def keys_by_recency(self) -> list[str]:
        """按"最旧 → 最新"返回全部键（诊断与测试用）。"""
        return list(self._data)
