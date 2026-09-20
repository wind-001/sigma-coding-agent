"""兄弟层 B。合法写法：只用更低层，不碰兄弟层 sib_a。"""

from __future__ import annotations

from mypkg.lower import BASE

B: str = BASE + "b"

__all__: list[str] = ["B"]
