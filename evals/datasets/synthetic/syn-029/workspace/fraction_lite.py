"""分数运算模块(评测工作区)。

FractionLite 用一对整数 ``numerator`` / ``denominator`` 表示分数,规则以本文档为准:

- ``FractionLite(numerator, denominator=1)``:构造时立即**规范化**——先按最大公约数
  约分到最简,再保证分母恒为正(分母为负时把符号搬到分子);``denominator`` 为
  0 时抛 ``ValueError``;
- ``numerator`` / ``denominator`` 属性返回规范化后的值;
- 算术运算 ``+`` ``-`` ``*`` ``/`` 均返回**新的、已规范化**的 FractionLite;
  除法按「乘以对方的倒数」实现,右操作数必须也是 FractionLite;
- 比较 ``==`` 与 ``<`` 按分数的**数值**进行,操作数可以是 FractionLite,也可以是
  int(如 ``FractionLite(4, 2) == 2``、``FractionLite(1, 2) < 1`` 均成立);
- 等值的分数有相同的哈希(可直接放入 set / 当 dict 键);
- ``__str__`` 返回规范化后的 ``"numerator/denominator"``,分母为 1 时只返回分子。
"""

from __future__ import annotations


class FractionLite:
    """最简分数:构造时约分,符号统一放在分子上。"""

    def __init__(self, numerator: int, denominator: int = 1) -> None:
        self._num = int(numerator)
        self._den = int(denominator)

    @property
    def numerator(self) -> int:
        """规范化后的分子。"""
        return self._num

    @property
    def denominator(self) -> int:
        """规范化后的分母(恒为正)。"""
        return self._den

    def __add__(self, other: "FractionLite") -> "FractionLite":
        return FractionLite(
            self._num * other._den + other._num * self._den,
            self._den * other._den,
        )

    def __sub__(self, other: "FractionLite") -> "FractionLite":
        return FractionLite(
            self._num * other._den - other._num * self._den,
            self._den * other._den,
        )

    def __mul__(self, other: "FractionLite") -> "FractionLite":
        return FractionLite(self._num * other._num, self._den * other._den)

    def __truediv__(self, other: "FractionLite") -> "FractionLite":
        return FractionLite(self._num * other._num, self._den * other._den)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FractionLite):
            return (self._num, self._den) == (other._num, other._den)
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, FractionLite):
            return (self._num, self._den) < (other._num, other._den)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._num, self._den))

    def __str__(self) -> str:
        if self._den == 1:
            return str(self._num)
        return f"{self._num}/{self._den}"
