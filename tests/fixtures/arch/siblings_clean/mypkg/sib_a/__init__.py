"""兄弟层 A。合法写法：只用更低层，不碰兄弟层 sib_b。"""

from __future__ import annotations

from mypkg.lower import BASE

A: str = BASE + "a"

__all__: list[str] = ["A"]
