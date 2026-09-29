"""``expr_lite`` 的行为测试。

运行方式(**必须 ``python -m pytest``**:它把 CWD 放进 ``sys.path``,
裸 ``pytest`` 不会,于是 ``import expr_lite`` 会失败):

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from expr_lite import evaluate, tokenize, to_rpn

# --- tokenize -----------------------------------------------------------------


def test_tokenize_basic() -> None:
    """多位数要作为整体切出来。"""
    assert tokenize("1+20*3") == ["1", "+", "20", "*", "3"]


def test_tokenize_ignores_spaces() -> None:
    assert tokenize(" 12 + 3 ") == ["12", "+", "3"]


def test_tokenize_rejects_bad_char() -> None:
    with pytest.raises(ValueError):
        tokenize("1 $ 2")


# --- to_rpn -------------------------------------------------------------------


def test_to_rpn_precedence() -> None:
    """乘法先出栈:2*3+4 是 2 3 * 4 +。"""
    assert to_rpn(["2", "*", "3", "+", "4"]) == ["2", "3", "*", "4", "+"]


def test_to_rpn_left_assoc() -> None:
    """同级左结合:8-3-2 是 (8-3)-2。"""
    assert to_rpn(["8", "-", "3", "-", "2"]) == ["8", "3", "-", "2", "-"]


def test_to_rpn_parens() -> None:
    assert to_rpn(["(", "1", "+", "2", ")", "*", "3"]) == ["1", "2", "+", "3", "*"]


def test_to_rpn_mixed() -> None:
    assert to_rpn(tokenize("2*(3+4)")) == ["2", "3", "4", "+", "*"]


# --- evaluate -----------------------------------------------------------------


def test_evaluate_single_number() -> None:
    assert evaluate("42") == 42


def test_evaluate_subtraction_order() -> None:
    """减法不是可交换的:9-2 必须是 7。"""
    assert evaluate("9-2") == 7


def test_evaluate_division_truncates() -> None:
    assert evaluate("9/2") == 4


def test_evaluate_division_toward_zero() -> None:
    """除法向零截断:-7/2 是 -3,不是 -4。"""
    assert evaluate("(2-9)/2") == -3


def test_evaluate_precedence() -> None:
    """乘加:先乘后加得 10,不能从左到右算成 14。"""
    assert evaluate("2*3+4") == 10


def test_evaluate_left_assoc() -> None:
    assert evaluate("100-30-20") == 50


def test_evaluate_parens() -> None:
    assert evaluate("(2+3)*4") == 20


def test_evaluate_nested_parens() -> None:
    assert evaluate("((1+2)*(3+4))") == 21


def test_evaluate_complex() -> None:
    assert evaluate("2*(3+(4-1)*2)") == 18


def test_evaluate_div_by_zero_raises() -> None:
    with pytest.raises(ZeroDivisionError):
        evaluate("1/0")


def test_evaluate_unbalanced_raises() -> None:
    with pytest.raises(ValueError):
        evaluate("2+3)")


def test_evaluate_incomplete_raises() -> None:
    with pytest.raises(ValueError):
        evaluate("")
