"""ratelimit_lite 令牌桶的验收测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ratelimit_lite import TokenBucket  # noqa: E402


class FakeClock:
    """可手动推进的时钟（时间注入）。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------- 初始状态与突发 ----------

def test_initial_bucket_is_full():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=3.0, clock=clock)
    assert b.try_acquire() is True
    assert b.try_acquire() is True
    assert b.try_acquire() is True


def test_burst_beyond_capacity_fails():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    assert b.try_acquire() is True
    assert b.try_acquire() is True
    assert b.try_acquire() is False


def test_default_clock_allows_initial_burst():
    assert TokenBucket(rate=1.0, capacity=1.0).try_acquire() is True


# ---------- 按时间恢复 ----------

def test_refill_by_elapsed():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    assert b.try_acquire(2.0) is True
    clock.advance(1.0)
    assert b.try_acquire() is True
    assert b.try_acquire() is False


def test_refill_fractional_ticks():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    assert b.try_acquire(2.0) is True
    clock.advance(0.5)
    assert b.try_acquire() is False
    clock.advance(0.5)
    assert b.try_acquire() is True


def test_refill_accumulates_exact_ticks():
    clock = FakeClock()
    b = TokenBucket(rate=2.0, capacity=3.0, clock=clock)
    assert b.try_acquire(3.0) is True
    for _ in range(4):
        clock.advance(0.25)
    assert b.try_acquire(2.0) is True
    assert b.try_acquire() is False


def test_refill_capped_at_capacity():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    clock.advance(100.0)
    assert b.try_acquire(3.0) is False  # 空闲再久，可用的也只有 capacity
    assert b.try_acquire(2.0) is True


def test_acquire_multiple_tokens_at_once():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=5.0, clock=clock)
    assert b.try_acquire(3.0) is True
    assert b.try_acquire(3.0) is False
    clock.advance(3.0)
    assert b.try_acquire(3.0) is True


# ---------- 失败不扣减 / 不推进时钟 ----------

def test_failed_acquire_does_not_consume():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    assert b.try_acquire(2.0) is True
    clock.advance(0.5)
    assert b.try_acquire() is False  # 只有 0.5 个，失败不扣
    clock.advance(0.5)
    assert b.try_acquire() is True   # 攒满 1.0 后成功


def test_acquire_does_not_advance_clock():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    before = clock.now
    b.try_acquire()
    b.try_acquire(5.0)
    assert clock.now == before


def test_available_is_settled_and_capped():
    clock = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    assert b.try_acquire() is True
    assert b.available() == 1.0
    clock.advance(100.0)
    assert b.available() == 2.0


# ---------- 时钟注入 ----------

def test_shared_clock_refills_both_buckets():
    clock = FakeClock()
    b1 = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    b2 = TokenBucket(rate=1.0, capacity=2.0, clock=clock)
    assert b1.try_acquire(2.0) is True
    clock.advance(1.0)
    assert b2.try_acquire() is True
    assert b1.try_acquire() is True


# ---------- 参数校验 ----------

def test_rejects_nonpositive_rate():
    with pytest.raises(ValueError):
        TokenBucket(rate=0.0, capacity=5.0)
    with pytest.raises(ValueError):
        TokenBucket(rate=-1.0, capacity=5.0)


def test_rejects_nonpositive_capacity():
    with pytest.raises(ValueError):
        TokenBucket(rate=1.0, capacity=0.0)
    with pytest.raises(ValueError):
        TokenBucket(rate=1.0, capacity=-2.0)
