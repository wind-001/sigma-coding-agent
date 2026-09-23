"""``dedupe`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import dedupe`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from dedupe import unique


# --- 默认：区分大小写，保序 ---------------------------------------------------


def test_removes_duplicates_keeping_first_order() -> None:
    assert unique(["b", "a", "b", "a"]) == ["b", "a"]


def test_case_sensitive_by_default() -> None:
    assert unique(["Ab", "ab"]) == ["Ab", "ab"]


def test_empty_list() -> None:
    assert unique([]) == []


def test_all_unique_passthrough() -> None:
    assert unique(["x", "y", "z"]) == ["x", "y", "z"]


# --- casefold：合并重复，但保留首现原样 ----------------------------------------


def test_casefold_merges_and_keeps_first_form() -> None:
    assert unique(["Ab", "aB", "AB"], casefold=True) == ["Ab"]


def test_casefold_single_pair_keeps_first() -> None:
    assert unique(["aB", "ab"], casefold=True) == ["aB"]


def test_casefold_handles_sharp_s() -> None:
    """casefold 连 ß 这类都归一：两份是同一个词，保留首现原样。"""
    assert unique(["Straße", "STRASSE"], casefold=True) == ["Straße"]


def test_casefold_off_does_not_merge() -> None:
    assert unique(["Ab", "aB", "AB"]) == ["Ab", "aB", "AB"]


def test_casefold_preserves_order_of_first_appearance() -> None:
    assert unique(["BB", "aa", "bb", "AA"], casefold=True) == ["BB", "aa"]
