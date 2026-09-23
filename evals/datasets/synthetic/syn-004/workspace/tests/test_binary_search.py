"""``binary_search`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import binary_search`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from binary_search import index_of, insertion_point


# --- index_of ---------------------------------------------------------------


def test_found_in_middle() -> None:
    assert index_of([1, 3, 5, 7, 9], 5) == 2


def test_found_first_element() -> None:
    assert index_of([1, 3, 5], 1) == 0


def test_found_last_element() -> None:
    assert index_of([1, 3, 5, 7], 7) == 3


def test_single_element_list() -> None:
    assert index_of([5], 5) == 0


def test_missing_returns_minus_one() -> None:
    assert index_of([1, 3, 5], 4) == -1


def test_missing_on_even_length() -> None:
    assert index_of([1, 3], 2) == -1


def test_empty_list_returns_minus_one() -> None:
    assert index_of([], 1) == -1


# --- insertion_point（bisect_left 语义）-------------------------------------


def test_insertion_point_before_first() -> None:
    assert insertion_point([2, 4, 6], 1) == 0


def test_insertion_point_in_middle() -> None:
    assert insertion_point([2, 4, 6], 3) == 1


def test_insertion_point_at_end() -> None:
    assert insertion_point([2, 4, 6], 7) == 3


def test_insertion_point_hits_first_duplicate() -> None:
    """target 已存在时，返回它第一个出现的位置。"""
    assert insertion_point([2, 4, 4, 6], 4) == 1
