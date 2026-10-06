"""mini_jsonpath:在嵌套结构里按路径取值。已支持 $.a.b 形式,* 选择器待补。"""

from __future__ import annotations

from typing import Any


def select(data: Any, path: str) -> Any:
    """path 形如 $.a.b ;缺失键抛 KeyError。"""
    if not path.startswith('$.'):
        raise ValueError(path)
    node: Any = data
    for part in path[2:].split('.'):
        if not part:
            continue
        if isinstance(node, dict):
            node = node[part]
        else:
            raise KeyError(part)
    return node
