"""简易 Markdown 内联链接解析器（评测工作区）。

只做一件事：从一段文本中找出全部内联链接 ``[文本](URL)``。规则以本文档为准：

- 链接形如 ``[文本](URL)``；``![文本](URL)`` 是图片，**不是链接**，要整段跳过，
  不产出任何结果；
- 反斜杠转义：紧跟在反斜杠后面的 ``[`` ``]`` ``(`` ``)`` ``!`` ``\\`` 是普通字符，
  不参与链接结构（``\\[`` 不开启链接，文本里的 ``\\]`` 不关闭链接，URL 里的
  ``\\)`` 不关闭 URL）；解析出的 text/url 要做反转义（``\\]`` 还原成 ``]``）；
  反斜杠后面跟其他字符时，反斜杠原样保留（``\\d`` 仍是 ``\\d``）；
- 链接文本内允许**成对嵌套**方括号：``[a [b] c](u)`` 的文本是 ``a [b] c``；
- URL 内允许**成对嵌套**圆括号：``[t](https://x.com/a_(b))`` 的 URL 是
  ``https://x.com/a_(b)``；
- 结构不完整的位置（有 ``[`` 但配不出 ``](...)``）按普通文本处理，从 ``[`` 的
  下一个字符继续向后扫描；
- 结果按出现顺序排列；``start`` 指向 ``[`` 的下标，``end`` 是 ``)`` 之后的位置
  （排他下标）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 允许被反斜杠转义的标点。
ESCAPABLE = frozenset("[]()!\\")


@dataclass(frozen=True)
class Link:
    """一条内联链接。text/url 已反转义；start/end 是原文中的区间（end 排他）。"""

    text: str
    url: str
    start: int
    end: int


def find_links(text: str) -> list[Link]:
    """按出现顺序返回 text 中的全部内联链接。"""
    links: list[Link] = []
    pos = 0
    while pos < len(text):
        open_idx = text.find("[", pos)
        if open_idx == -1:
            break
        close_idx = text.find("]", open_idx + 1)
        if close_idx == -1:
            pos = open_idx + 1
            continue
        if close_idx + 1 < len(text) and text[close_idx + 1] == "(":
            paren_end = text.find(")", close_idx + 2)
            if paren_end != -1:
                links.append(
                    Link(
                        text=_unescape(text[open_idx + 1 : close_idx]),
                        url=_unescape(text[close_idx + 2 : paren_end]),
                        start=open_idx,
                        end=paren_end + 1,
                    )
                )
                pos = paren_end + 1
                continue
        pos = open_idx + 1
    return links


def _unescape(s: str) -> str:
    """把 ``\\X``（X 属于可转义标点）还原成 ``X``；其余反斜杠原样保留。"""
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in ESCAPABLE:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)
