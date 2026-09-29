"""``roman_lite`` 的行为测试。

运行方式(**必须 ``python -m pytest``**:它把 CWD 放进 ``sys.path``,
裸 ``pytest`` 不会,于是 ``import roman_lite`` 会失败):

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from roman_lite import from_roman, to_roman

# --- to_roman -----------------------------------------------------------------


def test_to_roman_small() -> None:
    assert to_roman(1) == "I"
    assert to_roman(3) == "III"


def test_to_roman_subtractive_units() -> None:
    """4 与 9 必须用减写,不能连写四个 I。"""
    assert to_roman(4) == "IV"
    assert to_roman(9) == "IX"


def test_to_roman_subtractive_tens() -> None:
    assert to_roman(40) == "XL"
    assert to_roman(90) == "XC"


def test_to_roman_1994() -> None:
    assert to_roman(1994) == "MCMXCIV"


def test_to_roman_max() -> None:
    assert to_roman(3999) == "MMMCMXCIX"


def test_to_roman_zero_raises() -> None:
    with pytest.raises(ValueError):
        to_roman(0)


def test_to_roman_negative_raises() -> None:
    with pytest.raises(ValueError):
        to_roman(-7)


def test_to_roman_above_max_raises() -> None:
    """上界之外要抛错,不能闷头吐 MMMM。"""
    with pytest.raises(ValueError):
        to_roman(4000)


# --- from_roman ---------------------------------------------------------------


def test_from_roman_additive() -> None:
    assert from_roman("VIII") == 8
    assert from_roman("MMXXVI") == 2026


def test_from_roman_subtractive_units() -> None:
    """减写对:小值在大值前要相减,IV 是 4 不是 6。"""
    assert from_roman("IV") == 4


def test_from_roman_subtractive_mixed() -> None:
    assert from_roman("MCMXCIV") == 1994


def test_from_roman_max() -> None:
    assert from_roman("MMMCMXCIX") == 3999


def test_from_roman_rejects_unknown_char() -> None:
    with pytest.raises(ValueError):
        from_roman("ABC")


def test_from_roman_rejects_over_repeat() -> None:
    """I/X/C/M 最多连写三个。"""
    with pytest.raises(ValueError):
        from_roman("IIII")


def test_from_roman_rejects_repeated_five() -> None:
    """V/L/D 从不重复。"""
    with pytest.raises(ValueError):
        from_roman("VV")


def test_from_roman_rejects_illegal_pair() -> None:
    """IL/IC/XD/XM/VX 这类都不是合法减对。"""
    with pytest.raises(ValueError):
        from_roman("IL")
    with pytest.raises(ValueError):
        from_roman("VX")


def test_from_roman_rejects_misplaced_subtrahend() -> None:
    """减对之后又冒出同量级的大值,同样不合法。"""
    with pytest.raises(ValueError):
        from_roman("CMM")


def test_from_roman_rejects_empty() -> None:
    with pytest.raises(ValueError):
        from_roman("")
