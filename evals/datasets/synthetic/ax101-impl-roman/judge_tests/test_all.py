import pytest

from roman import from_roman, to_roman


def test_fourteen() -> None:
    assert to_roman(14) == 'XIV'


def test_mcmxciv() -> None:
    assert from_roman('MCMXCIV') == 1994


def test_max() -> None:
    assert to_roman(3999) == 'MMMCMXCIX'


def test_one() -> None:
    assert to_roman(1) == 'I'


def test_nine_hundred() -> None:
    assert to_roman(944) == 'CMXLIV'


def test_zero_raises() -> None:
    with pytest.raises(ValueError):
        to_roman(0)


def test_over_raises() -> None:
    with pytest.raises(ValueError):
        to_roman(4000)


def test_four_i_raises() -> None:
    with pytest.raises(ValueError):
        from_roman('IIII')


def test_lowercase_raises() -> None:
    with pytest.raises(ValueError):
        from_roman('xiv')


def test_empty_raises() -> None:
    with pytest.raises(ValueError):
        from_roman('')


def test_roundtrip() -> None:
    for n in range(1, 4000):
        assert from_roman(to_roman(n)) == n
