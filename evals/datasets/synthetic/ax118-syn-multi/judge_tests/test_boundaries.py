import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN = {
    'formatter.py': '91365da66b7df496bf66bd3bcd74a853255b7fa26dbd9ca2f76db52dd7ff831f',
}


def test_formatter_untouched() -> None:
    assert hashlib.sha256((ROOT / 'formatter.py').read_bytes()).hexdigest() == FROZEN['formatter.py']


def test_api_untouched() -> None:
    src = (ROOT / 'api.py').read_text(encoding='utf-8')
    assert 'username' not in src
