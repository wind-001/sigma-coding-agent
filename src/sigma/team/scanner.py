"""系统扫描器(非 agent,纯代码守护):两个纯函数 tick,零 I/O、零 sigma 内部依赖。

为什么扫描器不能交给任何 agent(星辰设计评审,详规 §2)
    它负责的事件——lease_expired / dep_succeeded / abandon(自动)——触发条件
    恰恰是"某个 agent 死了或没响应"。指望 agent 自己发现死亡**不闭环**:
    派发器/lead 也可能死。所以这一类事件由体系之外的几行轮询代码发出,
    调用者恒为 ``system:scanner``(角色分权:扫描器不是 agent)。

调用约定
    纯函数:吃一个 :class:`~sigma.team.board.Board`,原地应用迁移,返回
    被改动的任务。**持久化归调用方**(引擎在 store 事务内调用本模块,
    正常退出自动落盘;异常分毫不动——与 store 的失败语义同构)。

两个 tick 的分工(详规 §2)
    :func:`lease_tick` —— 时间驱动:running 且 now > lease_deadline → 回
    pending(attempts+1)。lease 是安全网不是主路径:worker 主动 fail 省掉
    整个 TTL 等待。
    :func:`dep_tick` —— 依赖与队列巡视:blocked 的 deps 全 success → 放行;
    上游进 dead/cancelled → 级联取消下游(迭代到不动点);fail 且 attempts
    达上限 → 自动 abandon 进 dead(消除 fail↔reclaim 死循环)。
"""

from __future__ import annotations

from sigma.team.board import (
    ABANDON,
    Board,
    deps_all_success,
    CANCEL,
    DEP_SUCCEEDED,
    LEASE_EXPIRED,
    SYSTEM_SCANNER,
    TeamTask,
    apply,
)


def lease_tick(board: Board, now: float) -> list[TeamTask]:
    """时间驱动回收:running 且 now > lease_deadline → pending(attempts+1)。

    ``lease_deadline == 0`` 的 running(理论上是引擎异常)同样视为过期——
    没有 lease 的 running 正是"没人看管"的定义。
    """
    expired = [
        task
        for task in board.tasks
        if task.state == "running" and now > task.lease_deadline
    ]
    return [
        apply(
            board,
            LEASE_EXPIRED,
            task_id=task.id,
            caller=SYSTEM_SCANNER,
            note="lease 超时回收(安全网;worker 主动汇报才是主路径)",
        )
        for task in expired
    ]


def dep_tick(board: Board) -> list[TeamTask]:
    """依赖放行 + 级联取消 + 自动放弃。返回被改动的任务(可能跨三类)。

    级联只作用于 **blocked/pending** 下游(详规 §5);已 running 的下游
    仅由 lead 决定去留——代码里体现为:级联的 CANCEL 守卫对 system 调用者
    放行 pending/blocked,running 的 CANCEL 守卫只认 lead。
    """
    changed: list[TeamTask] = []

    # 放行:blocked 的 deps 全 success → pending
    for task in list(board.tasks):
        if task.state == "blocked" and deps_all_success(board, task.deps):
            changed.append(
                apply(
                    board,
                    DEP_SUCCEEDED,
                    task_id=task.id,
                    caller=SYSTEM_SCANNER,
                )
            )

    # 级联 + 自动放弃:迭代到不动点(级联产生的 cancelled 可能触发新一级)
    while True:
        touched = False
        for task in list(board.tasks):
            if task.state == "fail" and task.attempts >= board.max_attempts:
                changed.append(
                    apply(
                        board,
                        ABANDON,
                        task_id=task.id,
                        caller=SYSTEM_SCANNER,
                        note="attempts 达上限,扫描器自动放弃",
                    )
                )
                touched = True
        for task in list(board.tasks):
            if task.state not in ("blocked", "pending"):
                continue
            upstream_dead = any(
                (dep := board.find(dep_id)) is not None
                and dep.state in ("dead", "cancelled")
                for dep_id in task.deps
            )
            if upstream_dead:
                changed.append(
                    apply(
                        board,
                        CANCEL,
                        task_id=task.id,
                        caller=SYSTEM_SCANNER,
                        note="上游任务 dead/cancelled,级联取消",
                    )
                )
                touched = True
        if not touched:
            break
    return changed
