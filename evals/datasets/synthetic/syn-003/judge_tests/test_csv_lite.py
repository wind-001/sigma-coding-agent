"""``csv_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import csv_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from csv_lite import parse_line


# --- 无引号的朴素情况 -------------------------------------------------------


def test_plain_line() -> None:
    assert parse_line("a,b,c") == ["a", "b", "c"]


def test_empty_line_gives_no_fields() -> None:
    assert parse_line("") == []


def test_empty_fields_are_preserved() -> None:
    assert parse_line("a,,c") == ["a", "", "c"]


def test_whitespace_is_not_trimmed() -> None:
    assert parse_line("a, b") == ["a", " b"]


def test_bare_quote_is_a_plain_character() -> None:
    """没有被成对包裹的引号就是普通字符。"""
    assert parse_line('a"b,c') == ['a"b', "c"]


# --- 引号包裹的字段 ---------------------------------------------------------


def test_quoted_field_may_contain_comma() -> None:
    assert parse_line('a,"b,c",d') == ["a", "b,c", "d"]


def test_doubled_quote_is_an_escaped_quote() -> None:
    assert parse_line('"say ""hi""",x') == ['say "hi"', "x"]


def test_fully_quoted_single_field() -> None:
    assert parse_line('"a,b"') == ["a,b"]
