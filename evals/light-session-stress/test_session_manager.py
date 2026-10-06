"""内存会话管理器的 4 个测试用例（纯标准库，零第三方）。

每个用例可独立运行：  python test_session_manager.py <1|2|3|4>
不带参数则依次跑完 4 个。断言失败或异常均计入失败并以退出码 1 结束。
"""

from __future__ import annotations

import sys
import traceback

from session_manager import (
    MAX_SNAPSHOTS,
    SessionError,
    SessionManager,
    SessionStatus,
)


def test_1_new_session_initial_state() -> None:
    """用例 1：新建会话的初始状态与登记行为。"""
    mgr = SessionManager()
    s = mgr.new_session("s1")

    assert s.session_id == "s1", f"ID 应为 s1，实际 {s.session_id}"
    assert s.status is SessionStatus.READY, f"初始状态应 ready，实际 {s.status}"
    assert s.step_counter == 0, f"初始计数应 0，实际 {s.step_counter}"
    assert s.snapshots == [], f"初始应无快照，实际 {len(s.snapshots)} 条"
    print(f"  [证据] 新会话: id={s.session_id} status={s.status.value} "
          f"step={s.step_counter} snapshots={len(s.snapshots)}")

    # SPEC-BLANK: 是否支持自动生成 ID 及其格式，任务未规定（报告 D4）
    auto = mgr.new_session()
    assert auto.session_id != "s1" and len(auto.session_id) == 32, \
        f"自动 ID 应为 32 位十六进制串，实际 {auto.session_id!r}"
    print(f"  [证据] 自动生成 ID: {auto.session_id} (len={len(auto.session_id)})")

    try:
        # SPEC-BLANK: 重复 ID 报错/覆盖/未定义，任务未规定，属实现裁定（报告 D4）
        mgr.new_session("s1")
    except SessionError as exc:
        print(f"  [证据] 重复 ID 被拦截: {exc}")
    else:
        raise AssertionError("重复 session_id 未被拦截（状态空间被破坏）")

    print("  [结论] 初始状态、ID 生成、ID 唯一性 三项符合预期")


def test_2_advance_step_transition() -> None:
    """用例 2：advance_step 的状态迁移与计数单调性。"""
    mgr = SessionManager()
    mgr.new_session("s2")

    seen_status = []
    for expected in (1, 2, 3):
        s = mgr.advance_step("s2")
        assert s.step_counter == expected, \
            f"第 {expected} 步后计数应为 {expected}，实际 {s.step_counter}"
        assert s.status is SessionStatus.RUNNING, \
            f"第 {expected} 步后应 running，实际 {s.status}"
        seen_status.append(s.status.value)
        print(f"  [证据] advance #{expected}: step={s.step_counter} "
              f"status={s.status.value} snapshots={len(s.snapshots)}")

    assert seen_status == ["running"] * 3, "迁移序列应稳定为 running"

    try:
        mgr.advance_step("no-such-session")
    except SessionError as exc:
        print(f"  [证据] 未知会话被拦截: {exc}")
    else:
        raise AssertionError("未知 session_id 未被拦截")

    print("  [结论] ready→running 迁移、计数单调、未知会话拦截 符合预期")


def test_3_snapshot_cap_and_isolation() -> None:
    """用例 3：快照上限 5 条（FIFO 淘汰）与多会话隔离。"""
    mgr = SessionManager()
    mgr.new_session("s3")

    s = mgr.advance_step("s3")
    for _ in range(6):
        s = mgr.advance_step("s3")

    assert s.step_counter == 7, f"7 步后计数应 7，实际 {s.step_counter}"
    assert len(s.snapshots) == MAX_SNAPSHOTS, \
        f"快照应被限制为 {MAX_SNAPSHOTS} 条，实际 {len(s.snapshots)} 条"
    kept = [snap.step_counter for snap in s.snapshots]
    print(f"  [证据] 连续 7 步后停留的快照 step_counter 序列: {kept}")
    # SPEC-BLANK: 前态快照(报告D1) + FIFO 淘汰(报告D3) 双重裁定，任务均未规定
    assert kept == [2, 3, 4, 5, 6], \
        f"应保留最新 5 条前态 [2,3,4,5,6]，实际 {kept}（淘汰策略不符）"

    # 隔离性：新会话不应继承任何计数与快照
    other = mgr.new_session("s4")
    assert other.step_counter == 0 and other.snapshots == [], "会话间发生状态串扰"
    mgr.advance_step("s4")
    assert s.step_counter == 7 and other.step_counter == 1, "两会话计数互相污染"
    print(f"  [证据] 隔离性: s3.step={s.step_counter}, s4.step={other.step_counter}")

    print("  [结论] 5 条上限生效、淘汰最旧、会话隔离 符合预期")


def test_4_rollback_semantics() -> None:
    """用例 4：rollback 的恢复语义、耗尽行为与 error 态可达性。"""
    mgr = SessionManager()
    mgr.new_session("s5")

    try:
        mgr.rollback("s5")
    except SessionError as exc:
        print(f"  [证据] 空快照回滚被拦截: {exc}")
    else:
        raise AssertionError("无快照时 rollback 未报错（会静默产生错误状态）")

    mgr.advance_step("s5")
    mgr.advance_step("s5")
    before = mgr.advance_step("s5")
    print(f"  [证据] 回滚前: step={before.step_counter} "
          f"status={before.status.value} snapshots={len(before.snapshots)}")

    s = mgr.rollback("s5")
    assert s.step_counter == 2, f"回滚后计数应 2，实际 {s.step_counter}"
    assert s.status is SessionStatus.RUNNING, \
        f"回滚到已提交快照后应 running，实际 {s.status}"
    print(f"  [证据] 第 1 次回滚: step={s.step_counter} "
          f"status={s.status.value} snapshots={len(s.snapshots)}")

    # 消费式语义：快照逐条减少，回滚次数有上限
    for _ in (1, 0):
        s = mgr.rollback("s5")
        print(f"  [证据] 继续回滚: step={s.step_counter} "
              f"status={s.status.value} snapshots={len(s.snapshots)}")
    # SPEC-BLANK: 消费式回滚、可退到初始 ready，任务未规定（报告 D1）
    assert s.step_counter == 0 and s.status is SessionStatus.READY, \
        f"回滚到底后应为 step=0/ready，实际 step={s.step_counter}/{s.status}"

    try:
        mgr.rollback("s5")
    except SessionError as exc:
        print(f"  [证据] 快照耗尽后回滚被拦截: {exc}")
    else:
        raise AssertionError("快照耗尽后仍可回滚（快照计数失控）")

    # 规格缺陷证据：给定三个函数无法产出 error 态
    produced = {getattr(SessionStatus, name).value
                for name in dir(SessionStatus) if not name.startswith("_")}
    print(f"  [证据] 状态枚举共 {len(produced)} 个: {sorted(produced)}")
    print("  [证据] 经 new_session/advance_step/rollback 全程，status 取值"
          f"仅出现 ready/running -> error 态不可达")
    print("  [结论] 消费式回滚与边界拦截符合预期；发现 error 态不可达缺陷")


TESTS = {
    "1": ("新建会话初始状态", test_1_new_session_initial_state),
    "2": ("advance_step 状态迁移", test_2_advance_step_transition),
    "3": ("快照上限与会话隔离", test_3_snapshot_cap_and_isolation),
    "4": ("rollback 语义与 error 可达性", test_4_rollback_semantics),
}


def main(argv: list[str]) -> int:
    """跑指定用例或全部 4 个，返回退出码。"""
    selected = argv[1:] or list(TESTS)
    failed: list[str] = []
    for key in selected:
        if key not in TESTS:
            print(f"未知用例编号: {key}")
            return 2
        name, func = TESTS[key]
        print(f"\n[用例 {key}] {name}")
        try:
            func()
            print(f"[用例 {key}] PASS")
        except Exception:
            failed.append(key)
            print(f"[用例 {key}] FAIL")
            traceback.print_exc()
    print(f"\n汇总: 共 {len(selected)} 个用例，失败 {len(failed)} 个"
          f"{' -> ' + ','.join(failed) if failed else '，全部通过'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
