"""taskq 任务队列的验收测试。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from taskq import Storage, TaskQueue  # noqa: E402


# ---------- 基础流转 ----------

def test_add_starts_pending():
    q = TaskQueue()
    t = q.add("写报告", priority=1)
    assert t.id == "t1"
    assert t.state == "pending"


def test_claim_empty_returns_none():
    assert TaskQueue().claim() is None


def test_claim_picks_highest_priority():
    q = TaskQueue()
    q.add("低", priority=1)
    q.add("高", priority=9)
    q.add("中", priority=5)
    t = q.claim()
    assert t is not None and t.title == "高" and t.state == "running"


def test_claim_same_priority_fifo_numeric():
    """同优先级按 id 数字先进先出：t2 早于 t10（不是字符串序 t10 < t2）。"""
    q = TaskQueue()
    for i in range(11):
        q.add(f"任务{i}", priority=3)
    first = q.claim()
    assert first is not None and first.id == "t1"
    second = q.claim()
    assert second is not None and second.id == "t2"


def test_claim_moves_to_running_then_exhausts():
    q = TaskQueue()
    q.add("唯一")
    assert q.claim() is not None
    assert q.claim() is None  # 唯一的任务已在 running，不再可取


def test_complete_marks_done():
    q = TaskQueue()
    t = q.add("活")
    q.claim()
    q.complete(t.id)
    assert t.state == "done"


# ---------- fail / 重试（attempts 计数） ----------

def test_fail_once_back_to_pending_with_count():
    q = TaskQueue()
    t = q.add("会失败的活")
    q.claim()
    q.fail(t.id)
    assert t.state == "pending"
    assert t.attempts == 1


def test_fail_three_times_gives_up():
    q = TaskQueue()
    t = q.add("反复失败")
    q.claim()
    q.fail(t.id)
    q.claim()
    q.fail(t.id)
    q.claim()
    q.fail(t.id)
    assert t.state == "failed"


def test_failed_task_never_claimed_again():
    q = TaskQueue()
    t = q.add("放弃的活")
    for _ in range(3):
        q.claim()
        q.fail(t.id)
    assert q.claim() is None


# ---------- 持久化 ----------

def test_save_then_load_roundtrip(tmp_path):
    q = TaskQueue()
    q.add("甲", priority=2)
    b = q.add("乙", priority=7)
    q.claim()
    q.complete(b.id)
    st = Storage(tmp_path / "q.json")
    q.save(st)
    q2 = TaskQueue.load(st)
    a2 = q2.get("t1")
    b2 = q2.get("t2")
    assert a2 is not None and a2.state == "pending" and a2.priority == 2
    assert b2 is not None and b2.state == "done"


def test_load_missing_file_is_empty(tmp_path):
    q = TaskQueue.load(Storage(tmp_path / "nope.json"))
    assert q.all_tasks() == []


def test_load_blocked_state_survives(tmp_path):
    """外部标记的 blocked 任务必须能加载回来，状态原样保留。"""
    path = tmp_path / "q.json"
    path.write_text(
        json.dumps(
            [{"id": "t1", "title": "等依赖", "priority": 0, "state": "blocked", "attempts": 0}]
        ),
        "utf-8",
    )
    q = TaskQueue.load(Storage(path))
    t = q.get("t1")
    assert t is not None and t.state == "blocked"
    assert q.claim() is None  # blocked 不可被 claim


def test_load_then_add_no_id_collision(tmp_path):
    """存储里缺号（t1、t3 存在，t2 被清理）后新增任务不得与任何已有 id 冲突。"""
    path = tmp_path / "q.json"
    path.write_text(
        json.dumps(
            [
                {"id": "t1", "title": "甲", "priority": 0, "state": "done", "attempts": 0},
                {"id": "t3", "title": "丙", "priority": 0, "state": "pending", "attempts": 0},
            ]
        ),
        "utf-8",
    )
    q = TaskQueue.load(Storage(path))
    fresh = q.add("新任务")
    assert fresh.id not in {"t1", "t3"}
    assert q.get(fresh.id) is fresh
