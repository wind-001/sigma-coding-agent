"""按显示宽度折行的小工具。

存在的理由：标准库 ``textwrap`` 按**字符数**折行，而中日韩字符在终端里占**两列**
——于是"折到 40 列"在纯中文文本上会溢出成 80 列宽。
日志格式化、终端表格、告警卡片这几处都用它。
"""

from __future__ import annotations


def display_width(text: str) -> int:
    """文本在等宽终端里占的列数。"""
    return len(text)


def wrap_text(text: str, width: int) -> list[str]:
    """把 ``text`` 折成若干行，每行**显示宽度**不超过 ``width``。

    空白字符按原样保留（本模块不负责折叠空白）。
    """
    if width <= 0:
        raise ValueError(f"width 必须为正数，收到 {width}")
    return [text[start : start + width] for start in range(0, len(text), width)]


def wrap_lines(lines: list[str], width: int) -> list[str]:
    """对多行文本逐行折行，落成一条扁平的列表。"""
    out: list[str] = []
    for line in lines:
        out.extend(wrap_text(line, width))
    return out
