"""迷你日志解析器：``<时间戳> <级别> <消息>`` 三段式单行日志。

约定：

- 时间戳形如 ``2026-09-23T19:05:00``（``YYYY-MM-DDTHH:MM:SS``，19 位），
  **只有以这种形状开头**（后跟一个空格）的行才算日志行；
- 级别统一归一成**大写**（``info`` 与 ``INFO`` 是同一种级别）；
- 消息里可以有任意空格，除级别前后的单个分隔空格外**原样保留**；
- 不以时间戳开头的行是上一条记录的**续行**：并入该记录的 ``message``
  （用 ``\n`` 连接）；出现在首条记录之前的杂散行直接忽略。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 日志行的时间戳形状。
TS_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


@dataclass
class LogRecord:
    """一条日志记录。"""

    ts: str
    level: str
    message: str


def parse_line(line: str) -> LogRecord | None:
    """解析单行；不是日志行（不以时间戳开头）返回 None。"""
    parts = line.split(" ")
    if len(parts) < 3:
        return None
    return LogRecord(ts=parts[0], level=parts[1], message=parts[2])


def parse_log(text: str) -> list[LogRecord]:
    """把整段日志聚成记录；续行并入上一条记录的 message。"""
    records: list[LogRecord] = []
    for line in text.splitlines():
        rec = parse_line(line)
        if rec is None:
            continue
        records.append(rec)
    return records


def count_by_level(text: str) -> dict[str, int]:
    """按（归一后的）级别统计条数。"""
    counts: dict[str, int] = {}
    for rec in parse_log(text):
        counts[rec.level] = counts.get(rec.level, 0) + 1
    return counts
