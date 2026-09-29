"""``argv_lite`` 的行为测试。

运行方式(**必须 ``python -m pytest``**:它把 CWD 放进 ``sys.path``,
裸 ``pytest`` 不会,于是 ``import argv_lite`` 会失败):

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from argv_lite import ArgvError, MissingValueError, ParseResult, UnknownOptionError, parse

# --- 开关 -----------------------------------------------------------------------


def test_short_flag() -> None:
    r = parse(["-v"])
    assert r.flags == ("verbose",)
    assert r.options == {}
    assert r.positionals == ()


def test_combined_short_flags() -> None:
    """-vn 等价于 -v -n。"""
    assert parse(["-vn"]).flags == ("verbose", "dry-run")


def test_combined_short_flags_dedupe() -> None:
    assert parse(["-vvv"]).flags == ("verbose",)


def test_flag_order_preserved() -> None:
    assert parse(["-n", "-v"]).flags == ("dry-run", "verbose")


def test_long_flag() -> None:
    assert parse(["--dry-run"]).flags == ("dry-run",)


# --- 值选项 ----------------------------------------------------------------------


def test_short_value() -> None:
    assert parse(["-o", "out.bin"]).options == {"output": "out.bin"}


def test_short_value_takes_option_like_arg() -> None:
    """值是紧随的下一个参数,即使它以 - 开头。"""
    assert parse(["-o", "--weird"]).options == {"output": "--weird"}


def test_short_value_canonical_name() -> None:
    assert parse(["-j", "2"]).options == {"jobs": "2"}


def test_long_value_attached() -> None:
    assert parse(["--output=a.bin"]).options == {"output": "a.bin"}


def test_long_value_with_equals_in_value() -> None:
    """``=`` 之后原样作为值,值本身可以再含 ``=``。"""
    assert parse(["--output=a=b.bin"]).options == {"output": "a=b.bin"}


def test_long_value_separate() -> None:
    assert parse(["--jobs", "4"]).options == {"jobs": "4"}


def test_missing_value_short_raises() -> None:
    with pytest.raises(MissingValueError):
        parse(["-o"])


def test_missing_value_long_raises() -> None:
    with pytest.raises(MissingValueError):
        parse(["--jobs"])


# --- 位置参数与 -- ---------------------------------------------------------------


def test_positionals_mixed() -> None:
    r = parse(["src", "-v", "dst"])
    assert r.positionals == ("src", "dst")
    assert r.flags == ("verbose",)


def test_lone_dash_is_positional() -> None:
    assert parse(["-"]).positionals == ("-",)


def test_double_dash_stops_options() -> None:
    """``--`` 之后的参数一律是位置参数,哪怕长得像选项。"""
    r = parse(["cmd", "--", "-v", "--x"])
    assert r.positionals == ("cmd", "-v", "--x")
    assert r.flags == ()


def test_double_dash_hides_value_option() -> None:
    assert parse(["--", "-o"]).positionals == ("-o",)


# --- 错误 -----------------------------------------------------------------------


def test_unknown_short_option_raises() -> None:
    with pytest.raises(UnknownOptionError):
        parse(["-x"])


def test_unknown_long_option_raises() -> None:
    with pytest.raises(UnknownOptionError):
        parse(["--config=x"])


def test_value_char_in_combined_raises() -> None:
    """组合串里只允许开关字符;``-vo`` 里的 ``o`` 不是开关。"""
    with pytest.raises(UnknownOptionError):
        parse(["-vo", "x"])


def test_errors_are_argv_errors() -> None:
    assert issubclass(UnknownOptionError, ValueError)
    assert issubclass(MissingValueError, ArgvError)
    assert issubclass(ArgvError, ValueError)
    assert parse(["-v"]) == ParseResult(flags=("verbose",), options={}, positionals=())
