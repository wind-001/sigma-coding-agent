class SlidingWindowRateLimiter:
    """滑动窗口限流:最近 window_seconds 秒内最多 max_events 次 allow() 通过。"""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self._max = max_events
        self._window = window_seconds
        self._events: list[float] = []

    def allow(self, now: float) -> bool:
        self._events = [t for t in self._events if now - t <= self._window]
        if len(self._events) >= self._max:
            return False
        self._events.append(now)
        return True
