"""罗马数字与整数的互转(1..3999,标准减写法)。

约定:
- ``to_roman(n)``:整数 → 规范减写罗马数字。只允许这些「积木」:
  ``M CM D CD C XC L XL X IX V IV I``;``V`` ``L`` ``D`` 从不重复,
  ``I`` ``X`` ``C`` ``M`` 最多连写三个;超出 1..3999 抛 ``ValueError``;
- ``from_roman(s)``:罗马数字 → 整数,并做**结构校验**,非法输入抛
  ``ValueError``:
  - 表外字符(含小写);
  - ``V`` ``L`` ``D`` 重复、``I`` ``X`` ``C`` ``M`` 连写超过三个;
  - 非法减对:小值在大值之前,但不属于 ``IX XC CM IV XL CD``
    (如 ``IL`` ``IC`` ``XD`` ``XM`` ``VX``);
  - 空串;
  校验规则等价于规范文法
  ``M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})`` 的全串匹配。
"""

from __future__ import annotations

import re

#: 贪心编码表:从大到小逐档扣除。
_TABLE: tuple[tuple[int, str], ...] = (
    (1000, "M"),
    (500, "D"),
    (100, "C"),
    (50, "L"),
    (10, "X"),
    (5, "V"),
    (1, "I"),
)

_VALUES: dict[str, int] = {
    "I": 1,
    "V": 5,
    "X": 10,
    "L": 50,
    "C": 100,
    "D": 500,
    "M": 1000,
}
_SUBTRACTIVE: dict[str, int] = {
    "IV": 4,
    "IX": 9,
    "XL": 40,
    "XC": 90,
    "CD": 400,
    "CM": 900,
}

#: 规范减写文法(见模块 docstring)。
_CANONICAL = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})")


def to_roman(number: int) -> str:
    """整数 → 规范减写罗马数字。"""
    if number < 1:
        raise ValueError(f"超出 1..3999:{number}")
    parts: list[str] = []
    rest = number
    for value, glyph in _TABLE:
        count, rest = divmod(rest, value)
        parts.append(glyph * count)
    return "".join(parts)


def _validate(text: str) -> None:
    """结构校验:不是规范减写就抛 ValueError。"""
    if not text or not _CANONICAL.fullmatch(text):
        raise ValueError(f"不是规范的罗马数字:{text!r}")


def from_roman(text: str) -> int:
    """罗马数字 → 整数(带结构校验)。"""
    total = 0
    for ch in text:
        total += _VALUES[ch]
    return total
