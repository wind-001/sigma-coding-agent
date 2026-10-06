from __future__ import annotations


class _Node:
    __slots__ = ('children', 'is_end')

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.is_end = False


class Trie:
    def __init__(self) -> None:
        self._root = _Node()

    def insert(self, word: str) -> None:
        node = self._root
        for ch in word:
            node = node.children.setdefault(ch, _Node())
        node.is_end = True

    def contains(self, word: str) -> bool:
        node = self._find(word)
        return node is not None and node.is_end

    def words_with_prefix(self, prefix: str) -> list[str]:
        node = self._find(prefix)
        if node is None:
            return []
        out: list[str] = []

        def walk(n: _Node, path: str) -> None:
            if n.is_end:
                out.append(path)
            for ch, child in n.children.items():
                walk(child, path + ch)

        walk(node, prefix)
        return sorted(out)

    def _find(self, s: str) -> _Node | None:
        node: _Node | None = self._root
        for ch in s:
            assert node is not None
            if ch not in node.children:
                return None
            node = node.children[ch]
        return node

    def remove(self, word: str) -> bool:
        node = self._root
        stack: list[tuple[_Node, str]] = []
        for ch in word:
            if ch not in node.children:
                return False
            stack.append((node, ch))
            node = node.children[ch]
        if not node.is_end:
            return False
        node.is_end = False
        # 自底向上清理:BUG 在这里——只看 children 为空就删,
        # 没有检查该节点是否仍是其他单词的结尾
        while stack:
            parent, ch = stack.pop()
            child = parent.children[ch]
            if not child.children:
                del parent.children[ch]
            else:
                break
        return True
