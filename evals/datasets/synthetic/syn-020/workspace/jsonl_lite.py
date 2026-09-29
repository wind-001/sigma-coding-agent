"""JSONL（每行一个 JSON 对象）的容错读取。

约定：
- ``parse_lines`` 逐行解析，返回 ``ParseResult(records, bad_lines)``；
- **纯空白的行静默跳过**——不算坏行、不进 records，但**占行号**；
- 行号从 1 起，按原文行计；
- 能被 json 解析但**不是对象**的行（数字 / 字符串 / 数组 / null…）算坏行，
  原因记 ``"not an object"``，不进 records；
- 解析失败的行尝试一次**尾逗号修复**：仅当（去掉行尾空白后）以 ``",}"``
  结尾时，把尾逗号去掉再解析一次；**修复不得改动行内其他内容**
  （字符串值里的 ``",}"`` 序列必须原样保留）；仍失败则记坏行，原因取异常消息；
- ``BadLine`` 记录 ``line_no`` / ``raw``（原始行）/ ``reason``；
- ``load(path)``：以 UTF-8 读文件后交给 ``parse_lines``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BadLine:
    line_no: int
    raw: str
    reason: str


@dataclass
class ParseResult:
    records: list[dict]
    bad_lines: list[BadLine]


def _repair_trailing_comma(line: str) -> str:
    """把紧贴收尾花括号的尾逗号去掉。"""
    return line.replace(",}", "}")


def parse_lines(text: str) -> ParseResult:
    """逐行容错解析 JSONL 文本，返回 records 与坏行清单。"""
    records: list[dict] = []
    bad_lines: list[BadLine] = []
    for line_no, raw in enumerate(text.splitlines()):
        try:
            line = _repair_trailing_comma(raw)
            value = json.loads(line)
            records.append(value)
        except ValueError as exc:
            bad_lines.append(BadLine(line_no=line_no, raw=raw, reason=str(exc)))
    return ParseResult(records=records, bad_lines=bad_lines)


def load(path: str | Path) -> ParseResult:
    """读取 UTF-8 编码的 JSONL 文件并容错解析。"""
    return parse_lines(Path(path).read_text("utf-8"))
