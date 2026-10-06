import shutil
from pathlib import Path

import pytest

import sorter

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def use_config():
    original = (ROOT / 'config.toml').read_bytes()

    def _write(strategy: str, reverse: bool) -> None:
        (ROOT / 'config.toml').write_text(
            f'[sort]\nstrategy = "{strategy}"\nreverse = {str(reverse).lower()}\n',
            encoding='utf-8')
    _write('length', False)
    yield _write
    (ROOT / 'config.toml').write_bytes(original)


def test_length_strategy(use_config) -> None:
    assert sorter.sort_items(['ccc', 'a', 'bb']) == ['a', 'bb', 'ccc']


def test_name_strategy(use_config) -> None:
    use_config('name', False)
    assert sorter.sort_items(['ccc', 'a', 'bb']) == ['a', 'bb', 'ccc']


def test_name_tie_alpha(use_config) -> None:
    use_config('length', False)
    assert sorter.sort_items(['bb', 'aa']) == ['aa', 'bb']


def test_reverse(use_config) -> None:
    use_config('name', True)
    assert sorter.sort_items(['ccc', 'a', 'bb']) == ['ccc', 'bb', 'a']


def test_length_tie_alpha() -> None:
    assert sorter.sort_items(['bb', 'aa']) == ['aa', 'bb']
