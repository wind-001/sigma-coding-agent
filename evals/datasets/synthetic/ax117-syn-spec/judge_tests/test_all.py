import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"total": 6, "by_level": {"DEBUG": 0, "INFO": 3, "WARNING": 1, "ERROR": 2}, "errors_first": ["连接超时", "磁盘满"]}


def test_report_exact() -> None:
    data = json.loads((ROOT / 'report.json').read_text(encoding='utf-8'))
    assert data == EXPECTED


def test_spec_untouched() -> None:
    text = (ROOT / 'SPEC.md').read_text(encoding='utf-8')
    assert 'errors_first' in text
