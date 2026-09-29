"""``pathlite`` 的行为测试。

运行方式(**必须 ``python -m pytest``**:它把 CWD 放进 ``sys.path``,
裸 ``pytest`` 不会,于是 ``import pathlite`` 会失败):

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from pathlite import is_under, join, normalize, relative_to

# --- normalize ----------------------------------------------------------------


def test_normalize_collapses_slashes() -> None:
    assert normalize("a//b///c") == "a/b/c"


def test_normalize_trailing_slash() -> None:
    assert normalize("a/b/") == "a/b"


def test_normalize_dots_dropped() -> None:
    assert normalize("./a/./b/.") == "a/b"


def test_normalize_parent_pops() -> None:
    assert normalize("a/b/../c") == "a/c"


def test_normalize_parent_overflow_kept() -> None:
    """相对路径越界的 ``..`` 要留在开头。"""
    assert normalize("../a") == "../a"


def test_normalize_parent_overflow_chain() -> None:
    assert normalize("a/../../b") == "../b"


def test_normalize_overflow_abs_discarded() -> None:
    """绝对路径到根之后,多余的 ``..`` 一律丢弃。"""
    assert normalize("/a/../../b") == "/b"


def test_normalize_empty_is_dot() -> None:
    assert normalize("") == "."
    assert normalize(".") == "."


def test_normalize_root() -> None:
    assert normalize("/") == "/"
    assert normalize("//") == "/"


# --- join ---------------------------------------------------------------------


def test_join_simple() -> None:
    assert join("a", "b", "c") == "a/b/c"


def test_join_absolute_resets() -> None:
    assert join("a", "/b", "c") == "/b/c"


def test_join_normalizes() -> None:
    assert join("a//b") == "a/b"
    assert join("a/", "b") == "a/b"


def test_join_parent() -> None:
    assert join("a", "..", "b") == "b"


def test_join_no_args() -> None:
    assert join() == ""


# --- relative_to / is_under ---------------------------------------------------


def test_relative_to_basic() -> None:
    assert relative_to("/a/b", "/a/b/c/d") == "c/d"


def test_relative_to_equal() -> None:
    assert relative_to("/a/b", "/a/b") == "."


def test_relative_to_rejects_sibling_prefix() -> None:
    """``/a/bc`` 只是共享字符串前缀,并不在 ``/a/b`` 之下。"""
    with pytest.raises(ValueError):
        relative_to("/a/b", "/a/bc")


def test_relative_to_rejects_outside() -> None:
    with pytest.raises(ValueError):
        relative_to("/a/b", "/a")


def test_relative_to_relative_paths() -> None:
    assert relative_to("x/y", "x/y/z") == "z"


def test_relative_to_mixed_abs_rel_raises() -> None:
    with pytest.raises(ValueError):
        relative_to("a", "/b")


def test_is_under_true() -> None:
    assert is_under("/a", "/a/b") is True


def test_is_under_false() -> None:
    assert is_under("/a", "/ab") is False


def test_is_under_mixed() -> None:
    assert is_under("a", "/b") is False
