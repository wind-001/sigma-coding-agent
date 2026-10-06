import pytest

from checksum import checksum


def test_basic() -> None:
    assert checksum(['a', 'b']) == 195


def test_empty_lines_skipped() -> None:
    assert checksum(['a', '', 'b']) == 195


def test_all_empty() -> None:
    assert checksum(['', '']) == 0


def test_empty_list() -> None:
    assert checksum([]) == 0
