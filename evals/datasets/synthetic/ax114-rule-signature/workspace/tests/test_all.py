from datediff import days_between


def test_same_day() -> None:
    assert days_between('2026-09-20', '2026-09-20') == 0


def test_within_month() -> None:
    assert days_between('2026-09-01', '2026-09-30') == 29


def test_across_month() -> None:
    assert days_between('2026-09-20', '2026-10-05') == 15


def test_negative() -> None:
    assert days_between('2026-10-05', '2026-09-20') == -15


def test_leap_year() -> None:
    assert days_between('2028-02-28', '2028-03-01') == 2
