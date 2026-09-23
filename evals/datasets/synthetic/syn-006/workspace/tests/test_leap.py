"""``leap`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import leap`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest
from leap import days_in_month, is_leap


# --- is_leap ----------------------------------------------------------------


def test_ordinary_leap_year() -> None:
    assert is_leap(2024) is True


def test_ordinary_non_leap_year() -> None:
    assert is_leap(2023) is False


def test_century_year_is_not_leap() -> None:
    """整除 100 但不整除 400：不闰。"""
    assert is_leap(1900) is False
    assert is_leap(2100) is False


def test_400_rule_wins() -> None:
    assert is_leap(2000) is True
    assert is_leap(2400) is True


# --- days_in_month ----------------------------------------------------------


def test_january_has_31_days() -> None:
    assert days_in_month(2023, 1) == 31


def test_february_in_leap_year() -> None:
    assert days_in_month(2024, 2) == 29


def test_february_in_century_year() -> None:
    assert days_in_month(1900, 2) == 28


def test_february_in_400_year() -> None:
    assert days_in_month(2000, 2) == 29


def test_month_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        days_in_month(2023, 13)
    with pytest.raises(ValueError):
        days_in_month(2023, 0)
