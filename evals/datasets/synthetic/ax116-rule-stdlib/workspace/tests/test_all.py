import pytest

from mini_jsonpath import select


def test_nested_key() -> None:
    assert select({'a': {'b': 1}}, '$.a.b') == 1


def test_missing_key_raises() -> None:
    with pytest.raises(KeyError):
        select({}, '$.a')


def test_bad_prefix_raises() -> None:
    with pytest.raises(ValueError):
        select({}, 'a.b')


def test_star_all() -> None:
    assert select({'a': [1, 2]}, '$.a[*]') == [1, 2]


def test_star_then_key() -> None:
    data = {'servers': [{'name': 'a'}, {'name': 'b'}]}
    assert select(data, '$.servers[*].name') == ['a', 'b']
