"""``jsched`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import jsched`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from jsched import CycleError, DepGraph, MinHeap, Scheduler  # noqa: E402


# ---------- MinHeap：基础 ----------


def test_pop_returns_smallest_priority() -> None:
    h = MinHeap()
    h.push(5, 1, "a")
    h.push(3, 2, "b")
    h.push(1, 3, "c")
    assert h.pop().payload == "c"
    assert h.pop().payload == "b"
    assert h.pop().payload == "a"


def test_peek_does_not_remove() -> None:
    h = MinHeap()
    h.push(4, 1, "a")
    h.push(2, 2, "b")
    assert h.peek().priority == 2
    assert len(h) == 2


def test_pop_empty_raises() -> None:
    with pytest.raises(IndexError):
        MinHeap().pop()


# ---------- MinHeap：堆序的正确性 ----------


def test_right_child_is_never_ignored() -> None:
    """下沉必须跟两个孩子都比：压入 3,9,5,7 后弹出顺序应是 3,5,7,9。

    只跟左孩子比的话，第二个弹出来的会是 7（5 被压在右子树里出不来）。
    """
    h = MinHeap()
    for priority in (3, 9, 5, 7):
        h.push(priority, priority, f"p{priority}")
    order = [h.pop().priority for _ in range(4)]
    assert order == [3, 5, 7, 9]


def test_equal_priority_is_fifo() -> None:
    """同优先级按 seq 先进先出，不能被堆的下沉顺序打乱。"""
    h = MinHeap()
    for seq, name in enumerate(("a", "b", "c", "d"), start=1):
        h.push(5, seq, name)
    order = [h.pop().payload for _ in range(4)]
    assert order == ["a", "b", "c", "d"]


# ---------- DepGraph：就绪判定 ----------


def test_ready_job_without_deps_is_always_ready() -> None:
    g = DepGraph()
    g.add_job("x")
    assert g.ready(set()) == {"x"}


def test_ready_chain_waits_for_predecessor() -> None:
    g = DepGraph()
    g.add_job("b", deps=["a"])
    assert g.ready(set()) == {"a"}
    assert g.ready({"a"}) == {"b"}


def test_ready_multi_dep_needs_all_done() -> None:
    """所有依赖都完成才算就绪，只完成一个不行。"""
    g = DepGraph()
    g.add_job("c", deps=["a", "b"])
    assert "c" not in g.ready({"a"})
    assert "c" in g.ready({"a", "b"})


def test_ready_excludes_done_jobs() -> None:
    g = DepGraph()
    g.add_job("x")
    assert g.ready({"x"}) == set()


# ---------- DepGraph：环检测 ----------


def test_has_cycle_detects_two_node_cycle() -> None:
    g = DepGraph()
    g.add_job("a", deps=["b"])
    g.add_job("b", deps=["a"])
    assert g.has_cycle() is True


def test_has_cycle_detects_self_loop() -> None:
    g = DepGraph()
    g.add_job("a", deps=["a"])
    assert g.has_cycle() is True


def test_has_cycle_detects_three_node_cycle() -> None:
    g = DepGraph()
    g.add_job("a", deps=["b"])
    g.add_job("b", deps=["c"])
    g.add_job("c", deps=["a"])
    assert g.has_cycle() is True


def test_has_cycle_diamond_is_acyclic() -> None:
    """菱形依赖（两条路都汇到 d）不是环。"""
    g = DepGraph()
    g.add_job("a", deps=["b", "c"])
    g.add_job("b", deps=["d"])
    g.add_job("c", deps=["d"])
    g.add_job("d")
    assert g.has_cycle() is False


# ---------- Scheduler：端到端顺序 ----------


def test_drain_respects_deps_over_priority() -> None:
    """依赖压过紧急度：b 再急也要等 a 先跑完。"""
    s = Scheduler()
    s.add("a", priority=5)
    s.add("b", priority=1, deps=["a"])
    assert s.drain() == ["a", "b"]


def test_drain_priority_among_independents() -> None:
    s = Scheduler()
    s.add("x", priority=3)
    s.add("y", priority=1)
    assert s.drain() == ["y", "x"]


def test_drain_equal_priority_fifo() -> None:
    """同优先级按登记顺序执行。"""
    s = Scheduler()
    for name in ("w", "x", "y", "z"):
        s.add(name, priority=2)
    assert s.drain() == ["w", "x", "y", "z"]


def test_drain_survives_hard_heap_shapes() -> None:
    """不同优先级的四个独立任务，弹出顺序要经得起堆的下沉。"""
    s = Scheduler()
    s.add("p3", priority=3)
    s.add("p9", priority=9)
    s.add("p5", priority=5)
    s.add("p7", priority=7)
    assert s.drain() == ["p3", "p5", "p7", "p9"]


def test_drain_multi_dep_job_waits_for_all() -> None:
    """c 依赖 a、b；a、b 之间按优先级排。"""
    s = Scheduler()
    s.add("a", priority=5)
    s.add("b", priority=4)
    s.add("c", priority=1, deps=["a", "b"])
    assert s.drain() == ["b", "a", "c"]


def test_drain_raises_cycle_error() -> None:
    s = Scheduler()
    s.add("a", deps=["b"])
    s.add("b", deps=["a"])
    with pytest.raises(CycleError):
        s.drain()
