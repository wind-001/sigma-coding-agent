import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN_HASHES = {
    'frozen/config.py': '360eeca6d1008605d81994b20ba8906d26009229736ee090530363b7e8f485ff',
    'frozen/defaults.json': 'd63a8d8f73e5be4733f66f4bd1f88c3502aaeeaa556ee3f129383684e9bd22da',
}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_frozen_zone_untouched() -> None:
    for rel, expected in FROZEN_HASHES.items():
        assert _sha(ROOT / rel) == expected, f'{rel} 被改动'


def test_app_untouched() -> None:
    src = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert 'parse_version' not in src, 'app.py 被改动'


def test_no_new_files_outside_legacy() -> None:
    top = {p.name for p in ROOT.iterdir() if p.name not in ('__pycache__', '.pytest_cache')}
    assert top == {'legacy', 'frozen', 'app.py', 'tests'}
