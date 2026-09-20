"""中间层。合法写法：引用更低一层。"""

from __future__ import annotations

from mypkg.layera import LOWER

MIDDLE: str = LOWER + "b"

__all__: list[str] = ["MIDDLE"]
