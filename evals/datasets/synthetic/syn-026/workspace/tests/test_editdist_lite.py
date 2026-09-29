"""``editdist_lite`` 的行为测试。

运行方式(**必须 ``python -m pytest``**:它把 CWD 放进 ``sys.path``,
裸 ``pytest`` 不会,于是 ``import editdist_lite`` 会失败):

    python -m pytest tests/ -q
"""

from __future__ import annotations

from editdist_lite import Step, distance, operations

# --- distance -----------------------------------------------------------------


def test_distance_both_empty() -> None:
    assert distance("", "") == 0


def test_distance_empty_target() -> None:
    """全删:边界值就是剩下多少个字符。"""
    assert distance("abc", "") == 3


def test_distance_empty_source() -> None:
    assert distance("", "abc") == 3


def test_distance_equal_strings() -> None:
    """相同的串距离为 0:相等的字符对不能收费。"""
    assert distance("abc", "abc") == 0


def test_distance_single_substitution() -> None:
    assert distance("a", "b") == 1


def test_distance_insertion() -> None:
    assert distance("cat", "cart") == 1


def test_distance_deletion() -> None:
    assert distance("cart", "cat") == 1


def test_distance_kitten_sitting() -> None:
    """教科书例子:答案是 3。"""
    assert distance("kitten", "sitting") == 3


def test_distance_flaw_lawn() -> None:
    assert distance("flaw", "lawn") == 2


# --- operations ---------------------------------------------------------------


def test_operations_substitution() -> None:
    """replace 记替换后的新字符(b 的字符)。"""
    assert operations("cat", "cut") == (
        Step("keep", "c"),
        Step("replace", "u"),
        Step("keep", "t"),
    )


def test_operations_insert() -> None:
    assert operations("cat", "cart") == (
        Step("keep", "c"),
        Step("keep", "a"),
        Step("insert", "r"),
        Step("keep", "t"),
    )


def test_operations_delete() -> None:
    """delete 记被删掉的字符(a 的字符)。"""
    assert operations("cart", "cat") == (
        Step("keep", "c"),
        Step("keep", "a"),
        Step("delete", "r"),
        Step("keep", "t"),
    )


def test_operations_insert_from_empty_source() -> None:
    assert operations("", "ab") == (
        Step("insert", "a"),
        Step("insert", "b"),
    )


def test_operations_delete_to_empty_target() -> None:
    assert operations("ab", "") == (
        Step("delete", "a"),
        Step("delete", "b"),
    )


def test_operations_tie_prefers_replace() -> None:
    """并列时按 replace → delete → insert 取先者,序列才唯一。"""
    assert operations("abc", "acd") == (
        Step("keep", "a"),
        Step("replace", "c"),
        Step("replace", "d"),
    )


def test_operations_multiple_inserts_in_order() -> None:
    assert operations("it", "item") == (
        Step("keep", "i"),
        Step("keep", "t"),
        Step("insert", "e"),
        Step("insert", "m"),
    )
