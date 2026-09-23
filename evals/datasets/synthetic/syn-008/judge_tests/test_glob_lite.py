"""``glob_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import glob_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from glob_lite import match


# --- 无通配符：退化为全等 -----------------------------------------------------


def test_exact_match() -> None:
    assert match("abc", "abc") is True


def test_exact_mismatch() -> None:
    assert match("abc", "abd") is False


def test_empty_pattern_and_empty_text() -> None:
    assert match("", "") is True


def test_empty_pattern_with_text() -> None:
    assert match("", "a") is False


# --- ? 恰好一个字符 -----------------------------------------------------------


def test_question_matches_one_char() -> None:
    assert match("a?c", "abc") is True


def test_question_requires_exactly_one() -> None:
    assert match("a?c", "ac") is False
    assert match("a?c", "abbc") is False


# --- * 任意个，含零个 ----------------------------------------------------------


def test_star_matches_suffix() -> None:
    assert match("a*", "abc") is True


def test_star_matches_nothing_at_end() -> None:
    assert match("a*", "a") is True


def test_star_matches_nothing_in_middle() -> None:
    assert match("a*b", "ab") is True


def test_star_matches_in_middle() -> None:
    assert match("a*b", "axxb") is True


def test_star_alone_matches_anything() -> None:
    assert match("*", "anything") is True


def test_star_alone_matches_empty() -> None:
    assert match("*", "") is True


def test_match_is_case_sensitive() -> None:
    assert match("A*", "abc") is False
