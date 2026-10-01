"""fraction_lite 分数模块的验收测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fraction_lite import FractionLite  # noqa: E402


# ---------- 构造与规范化 ----------

def test_constructor_reduces_by_gcd():
    f = FractionLite(2, 4)
    assert f.numerator == 1
    assert f.denominator == 2
    g = FractionLite(9, 6)
    assert g.numerator == 3
    assert g.denominator == 2


def test_constructor_reduces_negative_gcd():
    f = FractionLite(-6, 8)
    assert f.numerator == -3
    assert f.denominator == 4


def test_sign_moved_to_numerator():
    f = FractionLite(1, -2)
    assert f.numerator == -1
    assert f.denominator == 2
    g = FractionLite(-3, -4)
    assert g.numerator == 3
    assert g.denominator == 4


def test_zero_normalizes_to_zero_over_one():
    f = FractionLite(0, 5)
    assert f.numerator == 0
    assert f.denominator == 1
    g = FractionLite(0, -3)
    assert g.numerator == 0
    assert g.denominator == 1


def test_rejects_zero_denominator():
    with pytest.raises(ValueError):
        FractionLite(1, 0)
    with pytest.raises(ValueError):
        FractionLite(0, 0)


def test_str_uses_canonical_form():
    assert str(FractionLite(2, 4)) == "1/2"
    assert str(FractionLite(3, 1)) == "3"
    assert str(FractionLite(1, -2)) == "-1/2"


def test_equal_fractions_hash_the_same():
    assert len({FractionLite(1, 2), FractionLite(2, 4)}) == 1


# ---------- 相等与比较 ----------

def test_eq_between_fractions():
    assert FractionLite(2, 4) == FractionLite(1, 2)
    assert FractionLite(-1, 2) == FractionLite(1, -2)
    assert FractionLite(1, 2) != FractionLite(1, 3)


def test_eq_with_int():
    assert FractionLite(4, 2) == 2
    assert FractionLite(0, 5) == 0
    assert 2 == FractionLite(4, 2)
    assert not (FractionLite(1, 2) == 1)


def test_lt_between_fractions():
    assert FractionLite(1, 3) < FractionLite(1, 2)
    assert FractionLite(-1, 2) < FractionLite(1, 3)
    assert not (FractionLite(1, 2) < FractionLite(1, 2))
    assert FractionLite(2, 5) < FractionLite(1, 2)


def test_lt_with_int():
    assert FractionLite(1, 2) < 1
    assert FractionLite(-1, 2) < 0
    assert not (FractionLite(3, 2) < 1)


def test_sort_uses_value_order():
    values = [
        FractionLite(1, 2),
        FractionLite(2, 5),
        FractionLite(1, -2),
        FractionLite(3, 2),
    ]
    assert sorted(values) == [
        FractionLite(1, -2),
        FractionLite(2, 5),
        FractionLite(1, 2),
        FractionLite(3, 2),
    ]


# ---------- 算术运算 ----------

def test_add_returns_reduced_sum():
    assert FractionLite(1, 4) + FractionLite(1, 2) == FractionLite(3, 4)
    assert str(FractionLite(1, 6) + FractionLite(1, 3)) == "1/2"


def test_sub_handles_negative_results():
    assert FractionLite(1, 2) - FractionLite(1, 3) == FractionLite(1, 6)
    assert FractionLite(1, 4) - FractionLite(3, 4) == FractionLite(-1, 2)


def test_mul_reduces_product():
    assert FractionLite(2, 3) * FractionLite(3, 4) == FractionLite(1, 2)


def test_truediv_multiplies_by_reciprocal():
    assert FractionLite(1, 2) / FractionLite(1, 4) == 2
    assert FractionLite(3, 4) / FractionLite(3, 2) == FractionLite(1, 2)


def test_chained_expression():
    assert (FractionLite(1, 2) + FractionLite(1, 3)) * FractionLite(6, 5) == 1


# ---------- 纯度 ----------

def test_arithmetic_does_not_mutate_operands():
    a = FractionLite(1, 2)
    b = FractionLite(1, 3)
    _ = a + b
    _ = a - b
    _ = a * b
    _ = a / b
    assert a.numerator == 1 and a.denominator == 2
    assert b.numerator == 1 and b.denominator == 3
