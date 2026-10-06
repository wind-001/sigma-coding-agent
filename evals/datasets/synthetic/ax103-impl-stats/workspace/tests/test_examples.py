import pytest

from descriptive import DescriptiveStats


def test_median_odd() -> None:
    assert DescriptiveStats([3, 1, 2]).median() == 2


def test_empty_raises() -> None:
    with pytest.raises(ValueError):
        DescriptiveStats([])
