import pytest

from legacy.parser import parse_version


def test_simple() -> None:
    assert parse_version('1.2.3') == (1, 2, 3)


def test_whitespace() -> None:
    assert parse_version(' 1.2.3 ') == (1, 2, 3)


def test_bad_segment_raises() -> None:
    with pytest.raises(ValueError):
        parse_version('1.x.3')


def test_wrong_arity_raises() -> None:
    with pytest.raises(ValueError):
        parse_version('1.2.3.4')


def test_pair_still_works() -> None:
    from legacy.parser import parse_pair
    assert parse_pair('retries=3') == ('retries', 3)
