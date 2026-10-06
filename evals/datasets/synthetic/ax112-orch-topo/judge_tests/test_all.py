from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ['cache', 'db', 'auth', 'http', 'app', 'util']


def read_order() -> list[str]:
    return [line.strip() for line in (ROOT / 'build_order.txt').read_text(encoding='utf-8').splitlines() if line.strip()]


def test_exact_deterministic_order() -> None:
    assert read_order() == EXPECTED


def test_all_modules_present() -> None:
    assert len(read_order()) == 6
