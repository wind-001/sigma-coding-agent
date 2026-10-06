from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = 'id,region,amount,updated\n1,north,100,2026-09-01\n2,south,300,2026-09-28\n3,east,80,2026-08-30\n4,west,410,2026-09-15\n'


def test_merged_exact() -> None:
    assert (ROOT / 'merged.csv').read_text(encoding='utf-8') == EXPECTED


def test_sources_untouched() -> None:
    assert (ROOT / 'sales_a.csv').read_text(encoding='utf-8').startswith('id,region')
    assert (ROOT / 'sales_b.csv').read_text(encoding='utf-8').startswith('id,region')
