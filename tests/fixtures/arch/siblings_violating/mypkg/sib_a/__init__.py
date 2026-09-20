"""兄弟层 A。违规写法：引用了兄弟层 sib_b。

layers 契约放行这个方向（sib_a 排在 sib_b 之上，属于向下依赖），
所以只有 independence 契约会报错。
"""

from __future__ import annotations

from mypkg.lower import BASE
from mypkg.sib_b import B

A: str = BASE + B

__all__: list[str] = ["A"]
