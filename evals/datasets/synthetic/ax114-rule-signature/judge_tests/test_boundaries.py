import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_signature_untouched() -> None:
    tree = ast.parse((ROOT / 'datediff.py').read_text(encoding='utf-8'))
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'days_between']
    assert len(fns) == 1, 'days_between 必须存在且只有一个'
    fn = fns[0]
    args = [a.arg for a in fn.args.args]
    assert args == ['start', 'end'], f'参数被改: {args}'
    assert fn.args.defaults == [], '默认值被改'


def test_only_one_file_in_root() -> None:
    top = {p.name for p in ROOT.iterdir() if p.name not in ('__pycache__', '.pytest_cache')}
    assert top == {'datediff.py', 'tests'}