"""最高层。与 clean fixture 完全一致。"""

from __future__ import annotations

from mypkg.layerb import MIDDLE

UPPER: str = MIDDLE + "c"

__all__: list[str] = ["UPPER"]
