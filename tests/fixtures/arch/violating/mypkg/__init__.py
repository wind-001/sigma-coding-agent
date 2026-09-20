"""契约测试用 fixture：一个故意违反分层约定的最小包树。

与 ../clean 的唯一差别在 layera/__init__.py：
最低层反过来引用了最高层。除此之外完全一致，
这样测试失败时原因只有一种解释。
"""

from __future__ import annotations

__all__: list[str] = []
