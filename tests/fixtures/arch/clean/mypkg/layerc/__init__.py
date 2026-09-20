"""最高层。合法写法：引用更低层。"""

from __future__ import annotations

from mypkg.layerb import MIDDLE

UPPER: str = MIDDLE + "c"

__all__: list[str] = ["UPPER"]
