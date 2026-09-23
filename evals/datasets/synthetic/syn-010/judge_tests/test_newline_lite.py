"""``newline_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import newline_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from newline_lite import normalize


# --- 单一风格 ----------------------------------------------------------------


def test_lf_is_unchanged() -> None:
    assert normalize("a\nb") == "a\nb"


def test_cr_alone_becomes_lf() -> None:
    assert normalize("a\rb") == "a\nb"


def test_crlf_becomes_lf() -> None:
    assert normalize("a\r\nb") == "a\nb"


# --- 混合输入 ----------------------------------------------------------------


def test_mixed_styles() -> None:
    assert normalize("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_trailing_crlf() -> None:
    assert normalize("x\r\n") == "x\n"


def test_consecutive_crlf() -> None:
    assert normalize("a\r\n\r\nb") == "a\n\nb"


# --- 边界 --------------------------------------------------------------------


def test_empty_string() -> None:
    assert normalize("") == ""


def test_no_newline_at_all() -> None:
    assert normalize("plain") == "plain"
