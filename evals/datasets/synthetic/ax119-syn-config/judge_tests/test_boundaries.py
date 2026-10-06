import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_signature_untouched() -> None:
    tree = ast.parse((ROOT / 'sorter.py').read_text(encoding='utf-8'))
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'sort_items']
    assert len(fns) == 1 and [a.arg for a in fns[0].args.args] == ['items']


def test_no_import_time_read() -> None:
    tree = ast.parse((ROOT / 'sorter.py').read_text(encoding='utf-8'))
    top_calls = [n for n in tree.body if isinstance(n, ast.Expr)
                 and not (isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
    assert top_calls == [], 'sorter.py 顶层不许有执行语句(config 必须调用时读取;模块 docstring 除外)'