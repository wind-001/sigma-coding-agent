"""``jsonl_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import jsonl_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from jsonl_lite import BadLine, ParseResult, load, parse_lines

TEXT = "\n".join(
    [
        '{"id": 1, "kind": "login"}',
        "",
        '{"id": 2, "kind": "buy", "amount": 3}',
        "not json at all",
        '{"id": 3,}',
        "   ",
        "[1, 2]",
        "42",
        '{"note": "value ,} inside"}',
    ]
)


# --- 好行 --------------------------------------------------------------------


def test_result_type() -> None:
    result = parse_lines(TEXT)
    assert isinstance(result, ParseResult)
    assert isinstance(result.bad_lines[0], BadLine)


def test_valid_lines_become_records_in_order() -> None:
    records = parse_lines(TEXT).records
    assert records[0] == {"id": 1, "kind": "login"}
    assert records[1] == {"id": 2, "kind": "buy", "amount": 3}
    assert records[2] == {"id": 3}


def test_trailing_comma_repaired() -> None:
    records = parse_lines('{"id": 3,}\n').records
    assert records == [{"id": 3}]


def test_string_value_with_comma_brace_untouched() -> None:
    """合法行不做任何修复，字符串值里的 ``,}`` 序列必须原样保留。"""
    records = parse_lines(TEXT).records
    assert records[3] == {"note": "value ,} inside"}


def test_repair_keeps_inner_comma_brace() -> None:
    """需要修复的行，修复也只碰行尾——字符串值里的 ``,}`` 不能被改。"""
    records = parse_lines('{"msg": "a,}",}\n').records
    assert records == [{"msg": "a,}"}]


# --- 坏行 --------------------------------------------------------------------


def test_blank_lines_are_skipped_silently() -> None:
    """纯空白行不算坏行、不进 records。"""
    result = parse_lines(TEXT)
    assert all(line.raw.strip() for line in result.bad_lines)


def test_line_numbers_start_at_one() -> None:
    bad = parse_lines(TEXT).bad_lines
    unparseable = [line for line in bad if line.raw == "not json at all"]
    assert len(unparseable) == 1 and unparseable[0].line_no == 4


def test_line_numbers_count_blank_lines() -> None:
    """行号按原文行计，被跳过的空白行也占行号。"""
    bad = parse_lines(TEXT).bad_lines
    array_line = [line for line in bad if line.raw == "[1, 2]"]
    number_line = [line for line in bad if line.raw == "42"]
    assert array_line and array_line[0].line_no == 7
    assert number_line and number_line[0].line_no == 8


def test_non_object_number_is_bad_line() -> None:
    result = parse_lines(TEXT)
    assert all(record != 42 for record in result.records)
    matches = [line for line in result.bad_lines if line.raw == "42"]
    assert len(matches) == 1 and matches[0].reason == "not an object"


def test_non_object_array_is_bad_line() -> None:
    result = parse_lines(TEXT)
    assert all(record != [1, 2] for record in result.records)
    matches = [line for line in result.bad_lines if line.raw == "[1, 2]"]
    assert len(matches) == 1 and matches[0].reason == "not an object"


def test_unrepairable_line_reported_with_reason() -> None:
    matches = [
        line for line in parse_lines(TEXT).bad_lines if line.raw == "not json at all"
    ]
    assert len(matches) == 1
    assert matches[0].line_no == 4
    assert matches[0].reason != ""


# --- 文件入口 ------------------------------------------------------------------


def test_load_from_file(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(TEXT + "\n", "utf-8")
    result = load(path)
    assert len(result.records) == 4
    assert sorted(line.line_no for line in result.bad_lines) == [4, 7, 8]
    assert result.records[2] == {"id": 3}
