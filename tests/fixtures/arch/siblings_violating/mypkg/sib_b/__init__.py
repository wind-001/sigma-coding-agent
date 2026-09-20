"""兄弟层 B。与 siblings_clean 完全一致。"""

from __future__ import annotations

from mypkg.lower import BASE

B: str = BASE + "b"

__all__: list[str] = ["B"]
