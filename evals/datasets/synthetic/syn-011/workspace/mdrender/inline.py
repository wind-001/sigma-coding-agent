"""行内元素解析：行内代码、链接、粗体、斜体。

约定：``parse_inline`` 收到的 ``escape`` 是 renderer 注入的 HTML 转义函数，
先转义再解析标记——否则生成出来的标签会被转义掉。
"""

import re

BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
ITALIC_RE = re.compile(r"\*(.+?)\*")
CODE_RE = re.compile(r"`([^`]+)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def parse_inline(text: str, escape) -> str:
    """把一行里的行内标记解析成 HTML 片段。"""
    text = escape(text)
    text = CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    text = LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text
    )
    text = ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", text)
    text = BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    return text
