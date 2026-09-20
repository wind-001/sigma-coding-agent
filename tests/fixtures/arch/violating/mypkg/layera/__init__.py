"""最低层。违规写法：反过来引用了最高层 layerc。

这就是整个 fixture 存在的意义——用来证明分层契约真的会拦。
"""

from __future__ import annotations

from mypkg.layerc import UPPER

LOWER: str = "a"

__all__: list[str] = ["LOWER", "UPPER"]
