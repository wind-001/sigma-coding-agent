"""mdlink_lite 链接解析的验收测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mdlink_lite import find_links  # noqa: E402


def links_of(text: str) -> list[tuple[str, str]]:
    return [(l.text, l.url) for l in find_links(text)]


# ---------- 基础 ----------

def test_plain_text_without_links():
    assert find_links("这里没有任何链接，只有普通的一句话。") == []


def test_simple_link():
    got = find_links("[文档](https://example.com)")
    assert len(got) == 1
    assert got[0].text == "文档"
    assert got[0].url == "https://example.com"


def test_link_offsets():
    text = "看[文档](https://example.com)第七节"
    got = find_links(text)
    assert len(got) == 1
    assert text[got[0].start : got[0].end] == "[文档](https://example.com)"


def test_two_links_in_order():
    pairs = links_of("先看[a](u1)再看[b](u2)")
    assert pairs == [("a", "u1"), ("b", "u2")]


def test_unclosed_bracket_is_not_a_link():
    assert find_links("[只有半个链接") == []


def test_bracket_without_paren_is_not_a_link():
    assert find_links("[文字] 后面没有括号") == []


# ---------- 转义 ----------

def test_escaped_open_bracket_is_literal():
    assert find_links("\\[提示](x)") == []


def test_escaped_close_bracket_inside_text():
    pairs = links_of("[a \\] b](u)")
    assert pairs == [("a ] b", "u")]


def test_backslash_before_other_chars_is_kept():
    pairs = links_of("[路径 \\d](u)")
    assert pairs == [("路径 \\d", "u")]


# ---------- 图片 ----------

def test_image_is_not_a_link():
    assert find_links("![logo](https://example.com/l.png)") == []


def test_image_between_two_links_is_skipped():
    pairs = links_of("[a](u1) ![b](u2.png) [c](u3)")
    assert pairs == [("a", "u1"), ("c", "u3")]


# ---------- 嵌套 ----------

def test_nested_brackets_in_text():
    pairs = links_of("[看 [细节] 说明](u)")
    assert pairs == [("看 [细节] 说明", "u")]


def test_nested_parens_in_url():
    pairs = links_of("[wiki](https://x.com/a_(b))")
    assert pairs == [("wiki", "https://x.com/a_(b)")]


def test_escaped_parens_in_url():
    pairs = links_of("[t](https://x.com/a_\\(b\\))")
    assert pairs == [("t", "https://x.com/a_(b)")]
