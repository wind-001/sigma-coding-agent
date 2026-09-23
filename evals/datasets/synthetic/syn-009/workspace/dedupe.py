"""保序去重。

``casefold=True`` 时按"不区分大小写"判定重复，但**保留首次出现的那份原样**
——调用方展示的是用户的原文，不是归一化后的样子。
比较用 ``str.casefold``（比 ``lower`` 更彻底，连 ß 这类都归一）。
"""

from __future__ import annotations


def unique(items: list[str], *, casefold: bool = False) -> list[str]:
    if not casefold:
        return list(dict.fromkeys(items))
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item.casefold(), None)
    return list(seen)
