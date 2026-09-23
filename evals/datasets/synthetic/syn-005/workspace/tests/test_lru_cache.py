"""``lru_cache`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import lru_cache`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest
from lru_cache import LRUCache


# --- 基本读写 ---------------------------------------------------------------


def test_get_returns_value() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    assert cache.get("a") == 1


def test_missing_key_returns_none() -> None:
    cache = LRUCache(2)
    assert cache.get("nope") is None


def test_overwrite_keeps_size() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 9)
    assert len(cache) == 2
    assert cache.get("a") == 9


def test_capacity_zero_raises() -> None:
    with pytest.raises(ValueError):
        LRUCache(0)


# --- 淘汰：最久"未被使用"，不是最久"未被写入" --------------------------------


def test_evicts_oldest_when_never_read() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None
    assert cache.get("c") == 3


def test_get_refreshes_recency() -> None:
    """读过的键不算最旧：put a, put b, get a, put c → 淘汰 b。"""
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")
    cache.put("c", 3)
    assert cache.get("a") == 1
    assert cache.get("b") is None


def test_put_refreshes_recency() -> None:
    """覆盖也算使用：put a, put b, put a, put c → 淘汰 b。"""
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 9)
    cache.put("c", 3)
    assert cache.get("a") == 9
    assert cache.get("b") is None


def test_recency_order_is_visible() -> None:
    cache = LRUCache(3)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    cache.get("a")
    cache.put("d", 4)
    """get 刷新过 a，于是淘汰的是 b：最旧 → 最新应为 c, a, d。"""
    assert cache.keys_by_recency() == ["c", "a", "d"]
