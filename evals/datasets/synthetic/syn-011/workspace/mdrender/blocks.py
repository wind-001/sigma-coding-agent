"""块级元素解析：标题、无序列表（含嵌套）、段落。

列表项语法：``- 内容``；嵌套项比父项多 **至少 2 个空格**缩进，
可以多层嵌套（2 空格一层）。
"""

import re

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
LIST_ITEM_RE = re.compile(r"^( *)- (.*)$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_blocks(text: str) -> list[dict]:
    """把 markdown 文本解析成块列表。

    块的三种类型：
    - ``{"type": "heading", "level": int, "raw": str}``
    - ``{"type": "list", "items": [item]}``，item = ``{"raw": str, "children": [item]}``
    - ``{"type": "paragraph", "raw": str}``（连续非空行合并成一段）
    """
    blocks: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        heading = HEADING_RE.match(line)
        if heading:
            blocks.append(
                {
                    "type": "heading",
                    "level": len(heading.group(1)),
                    "raw": heading.group(2),
                }
            )
            i += 1
            continue
        if LIST_ITEM_RE.match(line):
            items, i = _parse_list(lines, i)
            blocks.append({"type": "list", "items": items})
            continue
        para = [line.strip()]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not HEADING_RE.match(lines[i])
            and not LIST_ITEM_RE.match(lines[i])
        ):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"type": "paragraph", "raw": " ".join(para)})
    return blocks


def _parse_list(lines: list[str], start: int) -> tuple[list[dict], int]:
    """解析一个列表，返回 (顶层 items 树, 下一行的下标)。

    嵌套规则：缩进比当前父项多 >=2 空格的行是子项；子项的子项要挂进
    **最近的一个父项**的 ``children`` 里，保持树形——不能把不同深度的
    嵌套都挂到同一层。
    """
    items: list[dict] = []
    base_indent = _indent_of(lines[start])
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            if i + 1 < len(lines) and LIST_ITEM_RE.match(lines[i + 1]):
                i += 1
                continue
            break
        m = LIST_ITEM_RE.match(line)
        if not m:
            break
        indent = _indent_of(line)
        if indent <= base_indent:
            items.append({"raw": m.group(2), "children": []})
        elif indent - base_indent >= 2:
            if not items:
                items.append({"raw": m.group(2), "children": []})
            else:
                items[-1]["children"].append({"raw": m.group(2), "children": []})
        i += 1
    return items, i
