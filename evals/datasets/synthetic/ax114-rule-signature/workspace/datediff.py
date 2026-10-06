from datetime import date


def days_between(start: str, end: str) -> int:
    """两个 ISO 日期(YYYY-MM-DD)之间的天数;start 晚于 end 时返回负数。"""
    s = date.fromisoformat(start[:7] + '-01')
    e = date.fromisoformat(end)
    return (e - s).days
