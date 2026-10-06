import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_signatures_untouched() -> None:
    tree = ast.parse((ROOT / 'payments.py').read_text(encoding='utf-8'))
    fns = {n.name: [a.arg for a in n.args.args] for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert fns.get('apply_discount') == ['price', 'percent']
    assert fns.get('refundable') == ['price', 'paid']
    assert fns.get('bulk_price') == ['unit', 'count']


def test_single_file() -> None:
    top = {p.name for p in ROOT.iterdir() if p.name not in ('__pycache__', '.pytest_cache')}
    assert top == {'payments.py', 'tests'}