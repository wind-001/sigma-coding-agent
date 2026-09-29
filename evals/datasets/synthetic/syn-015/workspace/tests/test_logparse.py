"""``logparse`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import logparse`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from logparse import count_by_level, parse_line, parse_log


# --- parse_line：三段切分与级别归一 -----------------------------------------


def test_parse_line_basic():
    rec = parse_line("2026-09-23T19:05:00 INFO 服务启动 完成")
    assert rec is not None
    assert rec.ts == "2026-09-23T19:05:00"
    assert rec.level == "INFO"
    assert rec.message == "服务启动 完成"


def test_parse_line_normalizes_level():
    rec = parse_line("2026-09-23T19:05:01 warn 磁盘 92%")
    assert rec is not None
    assert rec.level == "WARN"
    assert rec.message == "磁盘 92%"


def test_parse_line_rejects_lines_without_timestamp():
    assert parse_line("hello world foo") is None
    assert parse_line("INFO only") is None
    assert parse_line("") is None


# --- parse_log：成段聚合与续行归属 ------------------------------------------


def test_parse_log_single_record():
    records = parse_log("2026-09-23T19:05:00 info 单条")
    assert len(records) == 1
    assert records[0].ts == "2026-09-23T19:05:00"


def test_parse_log_multiple_records():
    text = (
        "2026-09-23T19:05:00 INFO 甲\n"
        "2026-09-23T19:05:01 ERROR 乙"
    )
    records = parse_log(text)
    assert [r.ts for r in records] == [
        "2026-09-23T19:05:00",
        "2026-09-23T19:05:01",
    ]


def test_parse_log_joins_continuation():
    text = (
        "2026-09-23T19:05:00 ERROR 部署失败\n"
        "  Traceback (most recent call last):\n"
        "  FileNotFoundError: cfg.ini\n"
        "2026-09-23T19:06:00 INFO 重试成功"
    )
    records = parse_log(text)
    assert len(records) == 2
    assert records[0].message == (
        "部署失败\n  Traceback (most recent call last):\n  FileNotFoundError: cfg.ini"
    )
    assert records[1].message == "重试成功"


def test_parse_log_skips_leading_junk():
    text = (
        "---- 会话开始 ----\n"
        "2026-09-23T19:05:00 INFO 服务启动\n"
        "收尾"
    )
    records = parse_log(text)
    assert len(records) == 1
    assert records[0].message == "服务启动\n收尾"


def test_parse_log_empty():
    assert parse_log("") == []
    assert parse_log("\n\n") == []


# --- count_by_level ---------------------------------------------------------


def test_count_by_level_all_upper():
    text = (
        "2026-09-23T19:05:00 INFO a\n"
        "2026-09-23T19:05:01 INFO b\n"
    )
    assert count_by_level(text) == {"INFO": 2}


def test_count_by_level_mixed_case():
    text = (
        "2026-09-23T19:05:00 info a\n"
        "2026-09-23T19:05:01 INFO b\n"
        "2026-09-23T19:05:02 warn c\n"
        "2026-09-23T19:05:03 ERROR d\n"
    )
    assert count_by_level(text) == {"INFO": 2, "WARN": 1, "ERROR": 1}


def test_count_by_level_ignores_noise():
    text = (
        "==== 巡检 ====\n"
        "2026-09-23T19:05:00 ERROR 1 号盘离线\n"
        "原因: 超时\n"
        "2026-09-23T19:05:01 error 2 号盘离线\n"
    )
    assert count_by_level(text) == {"ERROR": 2}


def test_count_by_level_empty():
    assert count_by_level("") == {}
