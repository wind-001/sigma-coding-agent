"""括号配对检查器：跳过字符串与注释后，检查 (), [], {} 是否配对。

供代码检查器、配置校验器使用。两步走：

1. ``code_only``：把字符串字面量与 ``#`` 注释替换成**等长**的空格
   （换行原样保留），只留下裸代码；字符串里的转义序列（如 ``\\"``）
   不结束字符串。
2. ``find_mismatch``：在裸代码上扫描三种括号，全部正确配对返回
   ``None``；否则返回一段能定位问题的描述（含出事位置，例如
   ``第 1 行的 '(' 未闭合``）。
"""

from __future__ import annotations

_PAIRS = {")": "(", "]": "[", "}": "{"}
_OPENERS = frozenset("([{")
_QUOTES = frozenset("\"'")


def code_only(text: str) -> str:
    """把字符串字面量与 ``#`` 注释替换成等长空格，换行原样保留。"""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "#":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch in _QUOTES:
            quote = ch
            out.append(" ")
            i += 1
            while i < n and text[i] != quote:
                out.append(" ")
                i += 1
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def find_mismatch(text: str) -> str | None:
    """扫描裸代码，返回第一处配对错误的描述；完全配对返回 None。"""
    stack: list[tuple[str, int]] = []
    line = 1
    for ch in code_only(text):
        if ch == "\n":
            line += 1
        elif ch in _OPENERS:
            stack.append((ch, line))
        elif ch in _PAIRS:
            if stack:
                stack.pop()
    return None
