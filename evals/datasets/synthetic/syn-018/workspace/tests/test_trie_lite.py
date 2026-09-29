"""``trie_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import trie_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trie_lite import Trie  # noqa: E402


# ---------- contains：整词才算 ----------


def test_contains_whole_word() -> None:
    t = Trie()
    t.insert("apple")
    assert t.contains("apple") is True


def test_contains_rejects_proper_prefix() -> None:
    """登记过 apple / appetite 之后，app 也只是前缀，不是词。"""
    t = Trie()
    t.insert("apple")
    t.insert("appetite")
    assert t.contains("app") is False


def test_contains_rejects_mid_path() -> None:
    t = Trie()
    t.insert("apple")
    assert t.contains("ap") is False


def test_contains_missing_words() -> None:
    t = Trie()
    t.insert("apple")
    assert t.contains("apx") is False
    assert t.contains("banana") is False
    assert Trie().contains("apple") is False


# ---------- starts_with：前缀路径 ----------


def test_starts_with_prefix_exists() -> None:
    t = Trie()
    t.insert("apple")
    assert t.starts_with("app") is True
    assert t.starts_with("apple") is True
    assert t.starts_with("") is True


def test_starts_with_missing() -> None:
    t = Trie()
    t.insert("apple")
    assert t.starts_with("apx") is False
    assert t.starts_with("banana") is False


# ---------- complete：自动补全 ----------


def test_complete_basic_sorted() -> None:
    """结果按字典序，与插入顺序无关。"""
    t = Trie()
    for word in ("apply", "apple", "appetite"):
        t.insert(word)
    assert t.complete("app") == ["appetite", "apple", "apply"]


def test_complete_prefix_itself_is_word() -> None:
    """前缀本身也是词时，它要出现在结果里。"""
    t = Trie()
    t.insert("app")
    t.insert("apple")
    assert t.complete("app") == ["app", "apple"]


def test_complete_empty_prefix_returns_all_sorted() -> None:
    t = Trie()
    for word in ("pear", "apple"):
        t.insert(word)
    assert t.complete("") == ["apple", "pear"]


def test_complete_single_char() -> None:
    t = Trie()
    for word in ("banana", "band", "apple"):
        t.insert(word)
    assert t.complete("b") == ["banana", "band"]


def test_complete_no_match() -> None:
    t = Trie()
    t.insert("apple")
    assert t.complete("zz") == []


# ---------- matches：单字符通配 ----------


def test_matches_literal_exact_length() -> None:
    t = Trie()
    t.insert("apple")
    assert t.matches("apple") == ["apple"]
    assert t.matches("appl") == []
    assert t.matches("apples") == []


def test_matches_dot_matches_exactly_one_char() -> None:
    t = Trie()
    for word in ("cat", "cot", "cut"):
        t.insert(word)
    assert t.matches("c.t") == ["cat", "cot", "cut"]


def test_matches_respects_length() -> None:
    t = Trie()
    t.insert("cat")
    t.insert("cats")
    assert t.matches("c.t") == ["cat"]
    assert t.matches("c...") == ["cats"]


def test_matches_all_dots_sorted() -> None:
    t = Trie()
    for word in ("dog", "cot", "cat"):
        t.insert(word)
    assert t.matches("...") == ["cat", "cot", "dog"]


def test_matches_no_match() -> None:
    t = Trie()
    t.insert("cat")
    assert t.matches("z.z") == []
    assert t.matches("d.g") == []
