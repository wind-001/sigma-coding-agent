"""``split_amount`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import split_amount`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest
from split_amount import allocate


# --- 分摊之后，总额一分都不能丢 -------------------------------------------


def test_sum_is_preserved() -> None:
    assert allocate(100, [1, 1, 1]) == [34, 33, 33]


def test_sum_is_preserved_uneven() -> None:
    assert sum(allocate(10, [1, 2, 3, 4])) == 10


def test_sum_is_preserved_with_ones() -> None:
    parts = allocate(5, [1, 1, 1, 1])
    assert sum(parts) == 5
    assert parts == [2, 1, 1, 1]


def test_tie_goes_to_the_earlier_weight() -> None:
    """余数相同（各 0.5）时，靠前的权重先得。"""
    assert allocate(5, [1, 1]) == [3, 2]


def test_each_part_within_one_of_ideal() -> None:
    weights = [3, 3, 3, 5, 7]
    parts = allocate(1000, weights)
    total_w = sum(weights)
    for part, w in zip(parts, weights):
        assert abs(part - 1000 * w / total_w) <= 1


def test_zero_weight_gets_zero() -> None:
    assert allocate(100, [0, 1, 1]) == [0, 50, 50]


# --- 非法输入：宁可报错，不要猜 -------------------------------------------


def test_empty_weights_raise() -> None:
    with pytest.raises(ValueError):
        allocate(100, [])


def test_negative_total_raises() -> None:
    with pytest.raises(ValueError):
        allocate(-1, [1])


def test_all_zero_weights_raise() -> None:
    with pytest.raises(ValueError):
        allocate(100, [0, 0])
