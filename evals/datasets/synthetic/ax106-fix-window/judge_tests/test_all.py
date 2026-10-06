from rate_limiter import SlidingWindowRateLimiter


def test_boundary_event_slides_out() -> None:
    rl = SlidingWindowRateLimiter(max_events=2, window_seconds=10)
    assert rl.allow(0.0) is True
    assert rl.allow(5.0) is True
    # t=0 已满 10s,恰好滑出 → 窗口内只剩 5.0 → 放行
    assert rl.allow(10.0) is True


def test_still_blocked_inside_window() -> None:
    rl = SlidingWindowRateLimiter(max_events=2, window_seconds=10)
    assert rl.allow(0.0) is True
    assert rl.allow(5.0) is True
    assert rl.allow(9.9) is False


def test_window_expiry_frees_slot() -> None:
    rl = SlidingWindowRateLimiter(max_events=1, window_seconds=10)
    assert rl.allow(0.0) is True
    assert rl.allow(10.0) is True
    assert rl.allow(19.0) is False
    assert rl.allow(20.0) is True


def test_single_event_window() -> None:
    rl = SlidingWindowRateLimiter(max_events=3, window_seconds=1)
    assert rl.allow(0.0) is True
    assert rl.allow(0.5) is True
    assert rl.allow(0.999) is True
    assert rl.allow(1.0) is True  # t=0 恰好滑出,仍只算 3 个
