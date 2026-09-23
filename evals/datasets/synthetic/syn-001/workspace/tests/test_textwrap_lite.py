"""``textwrap_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import textwrap_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest
from textwrap_lite import display_width, wrap_lines, wrap_text


# --- display_width：列数，不是字符数 ---------------------------------------


def test_ascii_is_one_column_per_char() -> None:
    assert display_width("abc") == 3


def test_cjk_is_two_columns_per_char() -> None:
    assert display_width("中文") == 4


def test_mixed_text_adds_up() -> None:
    assert display_width("a中b") == 4


# --- wrap_text：每行不超过给定**列数** -------------------------------------


def test_wrap_ascii() -> None:
    assert wrap_text("abcdef", 3) == ["abc", "def"]


def test_wrap_cjk_respects_column_width() -> None:
    """4 列预算下，"中文字符" 应当折成两行、每行两个字。"""
    assert wrap_text("中文字符", 4) == ["中文", "字符"]


def test_wrap_mixed_text_fits_in_one_line() -> None:
    assert wrap_text("a中b", 4) == ["a中b"]


def test_wrap_never_exceeds_width() -> None:
    """不变量：任意输入下，每一行的显示宽度都不超过 width。"""
    for width in (2, 3, 4, 6):
        for line in wrap_text("中文字符与ascii混排", width):
            assert display_width(line) <= width, f"width={width} 时折出了 {line!r}"


def test_wrap_lines_flattens() -> None:
    assert wrap_lines(["abcdef", "gh"], 3) == ["abc", "def", "gh"]


def test_width_must_be_positive() -> None:
    with pytest.raises(ValueError):
        wrap_text("abc", 0)
