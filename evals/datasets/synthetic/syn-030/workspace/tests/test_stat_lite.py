"""stat_lite 样本统计的验收测试。"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stat_lite import mean, median, percentile, stdev, variance  # noqa: E402


# ---------- mean ----------

def test_mean_basic():
    assert mean([1, 2, 3, 4]) == pytest.approx(2.5)
    assert mean([7]) == pytest.approx(7.0)


def test_mean_empty_raises():
    with pytest.raises(ValueError):
        mean([])


# ---------- median ----------

def test_median_odd_unsorted():
    assert median([3, 1, 2]) == pytest.approx(2.0)


def test_median_even_unsorted():
    assert median([4, 1, 3, 2]) == pytest.approx(2.5)


def test_median_empty_raises():
    with pytest.raises(ValueError):
        median([])


def test_median_does_not_mutate_input():
    xs = [3, 1, 2]
    median(xs)
    assert xs == [3, 1, 2]


# ---------- variance / stdev ----------

def test_variance_is_sample_variance():
    # 偏差平方和 = 5.0；样本方差 = 5 / 3，而不是 5 / 4
    assert variance([1, 2, 3, 4]) == pytest.approx(5.0 / 3.0)


def test_variance_known_example():
    # 经典例子：均值 5，偏差平方和 32，样本方差 32 / 7
    assert variance([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(32.0 / 7.0)


def test_variance_requires_two_points():
    with pytest.raises(ValueError):
        variance([1.0])
    with pytest.raises(ValueError):
        variance([])


def test_stdev_is_sqrt_of_sample_variance():
    assert stdev([1, 2, 3, 4]) == pytest.approx(math.sqrt(5.0 / 3.0))


def test_stdev_known_example():
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(math.sqrt(32.0 / 7.0))


def test_stdev_two_identical_points():
    assert stdev([5, 5]) == pytest.approx(0.0)


# ---------- percentile ----------

def test_percentile_sorts_unordered_input():
    assert percentile([10, 2, 8, 4, 6], 50) == pytest.approx(6.0)


def test_percentile_linear_interpolation():
    # rank = 0.25 * 3 = 0.75 → 1 + 0.75 * (2 - 1) = 1.75
    assert percentile([1, 2, 3, 4], 25) == pytest.approx(1.75)


def test_percentile_quartile_interpolation():
    # rank = 0.75 * 7 = 5.25 → 6 + 0.25 * (7 - 6) = 6.25
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8], 75) == pytest.approx(6.25)


def test_percentile_zero_and_hundred_are_min_max():
    assert percentile([3, 1, 2], 0) == pytest.approx(1.0)
    assert percentile([3, 1, 2], 100) == pytest.approx(3.0)


def test_percentile_agrees_with_median():
    assert percentile([1, 2, 3, 4, 5], 50) == pytest.approx(median([1, 2, 3, 4, 5]))


def test_percentile_rejects_out_of_range_p():
    with pytest.raises(ValueError):
        percentile([1, 2, 3], -0.1)
    with pytest.raises(ValueError):
        percentile([1, 2, 3], 100.1)
