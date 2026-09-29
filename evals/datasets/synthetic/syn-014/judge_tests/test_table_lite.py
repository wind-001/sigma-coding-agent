"""``table_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import table_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest
from table_lite import format_cell, render_table, truncate


# --- format_cell：补空格的方向 ---------------------------------------------


def test_format_left():
    assert format_cell("ab", 4) == "ab  "


def test_format_right():
    assert format_cell("ab", 4, "right") == "  ab"


def test_format_center_odd_padding_goes_right():
    # width 5，"ab" 差 3 格：左 1 右 2，多出的那格在右边。
    assert format_cell("ab", 5, "center") == " ab  "


# --- truncate：省略号必须占在上限之内 ---------------------------------------


def test_truncate_short_unchanged():
    assert truncate("abc", 6) == "abc"


def test_truncate_exact_cap_unchanged():
    assert truncate("abcdef", 6) == "abcdef"


def test_truncate_keeps_total_within_cap():
    assert truncate("confidential", 6) == "con..."


def test_truncate_rejects_cap_below_four():
    with pytest.raises(ValueError):
        truncate("ab", 3)


# --- render_table：整表 -----------------------------------------------------


def test_render_basic():
    out = render_table(["name", "qty"], [["pen", "3"], ["notebook", "12"]])
    assert out == "\n".join(
        [
            "name     | qty",
            "-" * 14,
            "pen      | 3  ",
            "notebook | 12 ",
        ]
    )


def test_render_header_wider_than_cells():
    out = render_table(["item", "n"], [["ab", "1"], ["cd", "2"]])
    assert out == "\n".join(
        [
            "item | n",
            "-" * 8,
            "ab   | 1",
            "cd   | 2",
        ]
    )


def test_render_center():
    out = render_table(["h1", "h2"], [["a", "b"]], aligns=["center", "center"])
    assert out == "\n".join(
        [
            "h1 | h2",
            "-" * 7,
            "a  | b ",
        ]
    )


def test_render_truncation():
    out = render_table(["name", "val"], [["configuration", "1"]], max_widths=[6, 0])
    assert out == "\n".join(
        [
            "name   | val",
            "-" * 12,
            "con... | 1  ",
        ]
    )


def test_render_empty_rows():
    assert render_table(["h"], []) == "h\n-"


def test_render_default_aligns_left():
    out = render_table(["a", "b"], [["x", "y"]])
    assert out == "\n".join(
        [
            "a | b",
            "-" * 5,
            "x | y",
        ]
    )
