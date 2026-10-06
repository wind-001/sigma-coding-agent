from brackets import is_balanced, longest_valid


def test_simple_balanced() -> None:
    assert is_balanced('()[]{}') is True


def test_longest_middle() -> None:
    assert longest_valid(')()())') == 4
