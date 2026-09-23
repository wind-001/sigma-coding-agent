"""解析单行 CSV 文本。

只做"一行 → 字段列表"这一件事，够日志抽样与配置行解析用。
引号规则与 RFC 4180 一致：

- 用一对双引号包裹的字段里**可以有逗号**；
- 字段内的双引号转义成两个连续双引号；
- 引号只在被包裹时才有特殊含义，裸露的引号就是普通字符；
- 不做空白裁剪：``a, b`` 的第二个字段就是 `` b``。
"""

from __future__ import annotations


def parse_line(line: str) -> list[str]:
    """把一行 CSV 文本拆成字段列表。空行返回空列表。"""
    if not line:
        return []
    return line.split(",")
