import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"DEBUG": 17, "INFO": 19, "WARNING": 16, "ERROR": 8}


def test_levels_json_exact() -> None:
    data = json.loads((ROOT / 'out' / 'levels.json').read_text(encoding='utf-8'))
    assert data == EXPECTED


def test_log_file_untouched() -> None:
    text = (ROOT / 'app.log').read_text(encoding='utf-8').splitlines()
    assert len(text) == 60
