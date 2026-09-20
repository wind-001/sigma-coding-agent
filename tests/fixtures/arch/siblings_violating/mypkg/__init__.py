"""兄弟层契约的违规样例。

与 ../siblings_clean 的唯一差别在 sib_a/__init__.py：
它引用了兄弟层 sib_b。除此之外完全一致。

注意 sib_a -> sib_b 在 layers 契约里是**合法的**（向下依赖），
所以这个样例只会让 independence 契约失败，不会污染 layers 契约的结果。
这样测试失败时，原因只有一种解释。
"""

from __future__ import annotations

__all__: list[str] = []
