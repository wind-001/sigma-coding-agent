from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {'checksum.py', 'notes.txt', 'tests'}


def test_no_extra_or_missing_files() -> None:
    top = {p.name for p in ROOT.iterdir() if p.name not in ('__pycache__', '.pytest_cache')}
    assert top == ALLOWED, f'文件集合被改动: {top}'


def test_no_new_imports() -> None:
    src = (ROOT / 'checksum.py').read_text(encoding='utf-8')
    assert 'import' not in src, 'checksum.py 不得引入 import'