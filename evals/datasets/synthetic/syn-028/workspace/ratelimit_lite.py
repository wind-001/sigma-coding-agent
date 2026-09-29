"""令牌桶限流器（评测工作区）。

TokenBucket 以恒定速率补充令牌、以桶容量限制突发。规则以本文档为准：

- ``TokenBucket(rate, capacity, clock=None)``：``rate`` 是每秒补充的令牌数，
  ``capacity`` 是桶容量（允许的最大突发）；两者必须 **> 0**，否则构造时抛
  ``ValueError``；
- 桶初始是**满**的（``capacity`` 个令牌）——刚建好的桶允许一次容量级突发；
- ``try_acquire(n=1)``：当前可用令牌 >= n 时扣掉 n 个并返回 True；否则返回
  False 且**不得扣减**任何令牌；
- 令牌随时间线性恢复（每秒 ``rate`` 个），任何时刻可用量都不超过 ``capacity``；
- 时间一律取自注入的 ``clock``（无参可调用对象，返回当前秒数）；缺省用
  ``time.monotonic``。try_acquire / available 只读时钟，绝不推进它；
- ``available()``：先把时间结算进来，再返回当前可用令牌数。
"""

from __future__ import annotations

import time
from typing import Callable

#: 时钟：返回当前时刻（秒）的无参可调用对象。
Clock = Callable[[], float]


class TokenBucket:
    """令牌桶：速率 rate 个/秒，容量 capacity，支持注入时钟。"""

    def __init__(self, rate: float, capacity: float, clock: Clock | None = None) -> None:
        self._rate = float(rate)
        self._capacity = float(capacity)
        self._clock: Clock = clock if clock is not None else time.monotonic
        self._tokens = 0.0
        self._last = self._clock()

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """取走 tokens 个令牌；够就扣掉并返回 True，不够返回 False。"""
        self._settle()
        self._tokens -= tokens
        return self._tokens >= 0

    def available(self) -> float:
        """当前可用令牌数（先把时间结算进来）。"""
        self._settle()
        return self._tokens

    def _settle(self) -> None:
        """按流逝时间补充令牌并推进结算时刻。"""
        now = self._clock()
        elapsed = now - self._last
        self._tokens = self._tokens + elapsed * self._rate
        self._last = now
