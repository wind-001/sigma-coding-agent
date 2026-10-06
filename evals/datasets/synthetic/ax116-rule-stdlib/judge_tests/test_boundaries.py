import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_only_stdlib_imports() -> None:
    for py in ROOT.glob('*.py'):
        tree = ast.parse(py.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split('.')[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split('.')[0]]
            for name in names:
                assert name in sys.stdlib_module_names, f'{py.name}: 非标准库 import {name}'


def test_single_file_root() -> None:
    top = {p.name for p in ROOT.iterdir() if p.name not in ('__pycache__', '.pytest_cache')}
    assert top == {'mini_jsonpath.py', 'tests'}