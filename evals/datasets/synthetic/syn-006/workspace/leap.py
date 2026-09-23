"""公历闰年与月份天数。

规则（公历）：

- 能被 4 整除的是闰年；
- 但能被 100 整除的**不闰**；
- 能被 400 整除的**又是闰年**。

``days_in_month`` 依赖同一条规则给出二月的天数。
"""

from __future__ import annotations

_MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def is_leap(year: int) -> bool:
    return year % 4 == 0


def days_in_month(year: int, month: int) -> int:
    if not 1 <= month <= 12:
        raise ValueError(f"月份必须在 1..12，收到 {month}")
    if month == 2 and is_leap(year):
        return 29
    return _MONTH_DAYS[month - 1]
