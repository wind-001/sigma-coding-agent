import pytest

from roman import from_roman, to_roman


def test_fourteen() -> None:
    assert to_roman(14) == 'XIV'


def test_mcmxciv() -> None:
    assert from_roman('MCMXCIV') == 1994
