"""前缀树（Trie）：自动补全与单字符通配匹配。

约定：
- ``insert`` 登记一个词条；只有**整词**算命中，前缀不算；
- ``complete(prefix)`` 返回所有以 prefix 开头的词，**按字典序**输出；
- ``matches(pattern)`` 返回所有与 pattern 等长、逐字符匹配的词
  （``.`` 匹配任意**一个**字符），同样按字典序输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    """树节点：``is_word`` 只在「某个词恰好到这里结束」时为真。"""

    children: dict[str, "Node"] = field(default_factory=dict)
    is_word: bool = False


class Trie:
    def __init__(self) -> None:
        self._root = Node()

    def insert(self, word: str) -> None:
        """登记一个词条。"""
        node = self._root
        for ch in word:
            node = node.children.setdefault(ch, Node())
            node.is_word = True
        node.is_word = True

    def contains(self, word: str) -> bool:
        """word 是否作为**完整词条**存在（前缀不算）。"""
        node = self._find(word)
        return node is not None

    def starts_with(self, prefix: str) -> bool:
        """树里是否存在以 prefix 开头的词（即这条前缀路径是否存在）。"""
        node = self._find(prefix)
        return node is not None

    def complete(self, prefix: str) -> list[str]:
        """所有以 prefix 开头的词，按字典序。"""
        start = self._find(prefix)
        if start is None:
            return []
        results: list[str] = []
        self._collect(start, prefix, results)
        return results

    def matches(self, pattern: str) -> list[str]:
        """所有与 pattern 等长、逐字符匹配（``.`` = 任意一个字符）的词，按字典序。"""
        results: list[str] = []
        self._match(self._root, pattern, "", results)
        results.sort()
        return results

    # ---- 内部 ----

    def _find(self, s: str) -> Node | None:
        node = self._root
        for ch in s:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    def _collect(self, node: Node, prefix: str, out: list[str]) -> None:
        if node.is_word:
            out.append(prefix)
        for ch, child in node.children.items():
            self._collect(child, ch + prefix, out)

    def _match(self, node: Node, pattern: str, acc: str, out: list[str]) -> None:
        if not pattern:
            if node.is_word:
                out.append(acc)
            return
        head, rest = pattern[0], pattern[1:]
        if head == ".":
            for ch, child in node.children.items():
                self._match(child, rest, acc + ch, out)
        else:
            child = node.children.get(head)
            if child is not None:
                self._match(child, rest, acc + head, out)
