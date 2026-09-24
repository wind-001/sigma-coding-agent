"""HTML 渲染：块列表 → HTML。"""

from mdrender.blocks import parse_blocks
from mdrender.inline import parse_inline


def escape_html(text: str) -> str:
    """HTML 文本转义。``&`` 必须最先转义，否则实体本身会被二次转义。"""
    return (
        text.replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_inline(raw: str) -> str:
    return parse_inline(raw, escape_html)


def _render_list(items: list[dict]) -> str:
    parts = ["<ul>"]
    for item in items:
        body = _render_inline(item["raw"])
        if item["children"]:
            body += _render_list(item["children"])
        parts.append(f"<li>{body}</li>")
    parts.append("</ul>")
    return "".join(parts)


def render_html(markdown: str) -> str:
    """渲染整个文档。块之间用换行分隔。"""
    parts: list[str] = []
    for block in parse_blocks(markdown):
        if block["type"] == "heading":
            level = block["level"]
            parts.append(
                f"<h{level}>{_render_inline(block['raw'])}</h{level}>"
            )
        elif block["type"] == "list":
            parts.append(_render_list(block["items"]))
        else:
            parts.append(f"<p>{_render_inline(block['raw'])}</p>")
    return "\n".join(parts)
