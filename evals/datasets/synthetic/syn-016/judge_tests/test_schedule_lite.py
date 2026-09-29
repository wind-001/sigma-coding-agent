"""``schedule_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import schedule_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from schedule_lite import (  # noqa: E402
    book,
    find_conflict,
    free_slots,
    merge_intervals,
)


# ---------- merge_intervals ----------


def test_merge_overlapping() -> None:
    assert merge_intervals([(1, 4), (3, 6)]) == [(1, 6)]


def test_merge_touching_joins() -> None:
    """首尾相接要并拢：[9,10) 与 [10,11) 是连续的。"""
    assert merge_intervals([(9, 10), (10, 11)]) == [(9, 11)]


def test_merge_chain_of_touching() -> None:
    assert merge_intervals([(1, 2), (2, 3), (3, 4)]) == [(1, 4)]


def test_merge_contained_keeps_outer() -> None:
    """被包含的区间不能把外层截短。"""
    assert merge_intervals([(1, 10), (2, 3)]) == [(1, 10)]


def test_merge_unsorted_input() -> None:
    assert merge_intervals([(5, 8), (1, 3), (2, 4)]) == [(1, 4), (5, 8)]


def test_merge_disjoint_stays_sorted() -> None:
    assert merge_intervals([(10, 12), (1, 2)]) == [(1, 2), (10, 12)]


def test_merge_empty() -> None:
    assert merge_intervals([]) == []


# ---------- find_conflict ----------


def test_conflict_overlap_detected() -> None:
    assert find_conflict([(9, 10), (14, 15)], 9, 11) == (9, 10)


def test_conflict_inside_detected() -> None:
    assert find_conflict([(9, 12)], 10, 11) == (9, 12)


def test_conflict_touching_after_is_free() -> None:
    """接在现有预约**之后**开始不算冲突（左闭右开）。"""
    assert find_conflict([(9, 10)], 10, 11) is None


def test_conflict_touching_before_is_free() -> None:
    """在现有预约**结束那一刻**之前结束不算冲突。"""
    assert find_conflict([(9, 10)], 8, 9) is None


# ---------- book ----------


def test_book_touching_slot_accepted() -> None:
    """紧挨着现有预约的下一个时段是合法预约。"""
    assert book([(9, 10)], 10, 11) == [(9, 10), (10, 11)]


def test_book_overlapping_raises() -> None:
    with pytest.raises(ValueError):
        book([(9, 10)], 9, 11)


def test_book_inserts_sorted() -> None:
    assert book([(10, 12)], 8, 9) == [(8, 9), (10, 12)]


def test_book_does_not_mutate_input() -> None:
    original = [(9, 10)]
    book(original, 11, 12)
    assert original == [(9, 10)]


# ---------- free_slots ----------


def test_free_slots_basic() -> None:
    assert free_slots([(10, 12)], 8, 18) == [(8, 10), (12, 18)]


def test_free_slots_no_bookings() -> None:
    """整天没预约，就返回整个窗口。"""
    assert free_slots([], 8, 18) == [(8, 18)]


def test_free_slots_touching_bookings() -> None:
    """相接的两段预约并拢后，中间不留假空档。"""
    assert free_slots([(9, 10), (10, 11)], 8, 12) == [(8, 9), (11, 12)]


def test_free_slots_fully_covered() -> None:
    assert free_slots([(8, 18)], 8, 18) == []


def test_free_slots_ignores_out_of_window() -> None:
    """窗口外的预约被忽略，剩下的整段都是空的。"""
    assert free_slots([(6, 7), (19, 20)], 8, 18) == [(8, 18)]
