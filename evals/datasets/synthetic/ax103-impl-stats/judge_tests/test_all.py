import pytest

from descriptive import DescriptiveStats


def test_median_odd() -> None:
    assert DescriptiveStats([3, 1, 2]).median() == 2


def test_median_even() -> None:
    assert DescriptiveStats([4, 1, 3, 2]).median() == 2.5


def test_empty_raises() -> None:
    with pytest.raises(ValueError):
        DescriptiveStats([])


def test_mode_tie_first_seen() -> None:
    assert DescriptiveStats([2, 1, 2, 1]).mode() == 2


def test_mode_simple() -> None:
    assert DescriptiveStats([5, 5, 1]).mode() == 5


def test_stdev_sample() -> None:
    assert abs(DescriptiveStats([2, 4, 4, 4, 5, 5, 7, 9]).stdev() - 2.13809) < 1e-4


def test_stdev_single_raises() -> None:
    with pytest.raises(ValueError):
        DescriptiveStats([1]).stdev()


def test_copy_semantics() -> None:
    src = [1, 2, 3]
    stats = DescriptiveStats(src)
    src.append(99)
    assert stats.median() == 2
