"""``template_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import template_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest
from template_lite import render


# --- 单占位符 ---------------------------------------------------------------


def test_single_placeholder() -> None:
    assert render("hi {{name}}", name="bot") == "hi bot"


def test_value_is_coerced_to_str() -> None:
    assert render("n={{n}}", n=3) == "n=3"


def test_text_without_placeholder_is_untouched() -> None:
    assert render("plain text") == "plain text"


def test_key_is_identifier_chars_only() -> None:
    assert render("{{user_name}}", user_name="Lin") == "Lin"


# --- 多占位符：互相独立，不能吞成一团 ----------------------------------------


def test_two_placeholders() -> None:
    assert render("{{a}} and {{b}}", a=1, b=2) == "1 and 2"


def test_repeated_key() -> None:
    assert render("{{x}}+{{x}}", x=7) == "7+7"


def test_adjacent_placeholders() -> None:
    assert render("{{a}}{{b}}", a="A", b="B") == "AB"


# --- 缺失的 key 必须炸出来 ---------------------------------------------------


def test_missing_key_raises() -> None:
    with pytest.raises(KeyError):
        render("hi {{name}}")


def test_missing_key_message_names_the_key() -> None:
    with pytest.raises(KeyError) as excinfo:
        render("hi {{name}}")
    assert "name" in str(excinfo.value)


def test_partial_missing_also_raises() -> None:
    """给了一个 key、缺另一个：同样要炸，不能渲染半份。"""
    with pytest.raises(KeyError):
        render("{{a}} and {{b}}", a=1)
