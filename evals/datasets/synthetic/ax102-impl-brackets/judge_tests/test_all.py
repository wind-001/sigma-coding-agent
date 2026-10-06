from brackets import is_balanced, longest_valid


def test_simple_balanced() -> None:
    assert is_balanced('()[]{}') is True


def test_nested() -> None:
    assert is_balanced('([{}])') is True


def test_cross_raises_false() -> None:
    assert is_balanced('([)]') is False


def test_ignore_other_chars() -> None:
    assert is_balanced('a(b)c[d]{e}') is True


def test_unclosed() -> None:
    assert is_balanced('([') is False


def test_empty_balanced() -> None:
    assert is_balanced('') is True


def test_longest_middle() -> None:
    assert longest_valid(')()())') == 4


def test_longest_head() -> None:
    assert longest_valid('(()') == 2


def test_longest_none() -> None:
    assert longest_valid('(') == 0


def test_longest_empty() -> None:
    assert longest_valid('') == 0


def test_longest_full() -> None:
    assert longest_valid('()()()') == 6
