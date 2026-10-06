import pytest

from payments import apply_discount, bulk_price, refundable


def test_discount_ninety() -> None:
    assert apply_discount(100, 90) == 90


def test_discount_zero() -> None:
    assert apply_discount(100, 0) == 0


def test_discount_no_change() -> None:
    assert apply_discount(77, 100) == 77


def test_refundable_yes() -> None:
    assert refundable(100, 120) is True


def test_refundable_exact() -> None:
    assert refundable(100, 100) is True


def test_refundable_no() -> None:
    assert refundable(100, 99) is False


def test_bulk_small() -> None:
    assert bulk_price(30, 5) == 150


def test_bulk_discount() -> None:
    assert bulk_price(30, 10) == 270


def test_bulk_discount_large() -> None:
    assert bulk_price(30, 12) == 324
