"""极简文件名匹配：``?`` 恰好一个字符，``*`` 任意个（**含零个**）字符。

比 fnmatch 更小的子集，只支持这两种通配符，用于白名单规则。
匹配**区分大小写**；没有通配符时退化为全等。
"""

from __future__ import annotations


def match(pattern: str, text: str) -> bool:
    if not pattern:
        return not text
    head, rest = pattern[0], pattern[1:]
    if head == "*":
        # * 至少吃掉一个字符
        return any(match(rest, text[i:]) for i in range(1, len(text) + 1))
    if not text:
        return False
    if head == "?" or head == text[0]:
        return match(rest, text[1:])
    return False
