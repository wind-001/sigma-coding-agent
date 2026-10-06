from trie import Trie


def test_remove_shared_prefix_no_collateral() -> None:
    t = Trie()
    t.insert('api')
    t.insert('apple')
    assert t.remove('api') is True
    assert t.contains('api') is False
    assert t.contains('apple') is True
    assert t.words_with_prefix('ap') == ['apple']


def test_remove_missing_returns_false() -> None:
    t = Trie()
    t.insert('cat')
    assert t.remove('dog') is False
    assert t.remove('ca') is False
    assert t.contains('cat') is True


def test_remove_tail_nodes_recycled() -> None:
    t = Trie()
    t.insert('hello')
    assert t.remove('hello') is True
    assert t.words_with_prefix('h') == []


def test_double_remove() -> None:
    t = Trie()
    t.insert('x')
    assert t.remove('x') is True
    assert t.remove('x') is False


def test_stem_is_also_word() -> None:
    t = Trie()
    t.insert('car')
    t.insert('carpet')
    assert t.remove('carpet') is True
    assert t.contains('car') is True
    assert t.words_with_prefix('car') == ['car']


def test_remove_longer_keeps_shorter_word() -> None:
    t = Trie()
    t.insert('app')
    t.insert('apple')
    assert t.remove('apple') is True
    assert t.contains('app') is True
    assert t.words_with_prefix('ap') == ['app']
