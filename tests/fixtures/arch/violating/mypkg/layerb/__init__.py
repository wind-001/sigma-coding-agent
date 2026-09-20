"""中间层。与 clean fixture 完全一致。"""

from __future__ import annotations

from mypkg.layera import LOWER

MIDDLE: str = LOWER + "b"

__all__: list[str] = ["MIDDLE"]
