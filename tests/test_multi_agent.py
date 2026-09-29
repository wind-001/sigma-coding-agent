"""multi_agent 引擎与 Scanner 的门禁测试(G-TEAM-8..13,详规 v3 §6)。

全部离线:引擎的 runner 用假实现(立即返回结论或挂起),clock 可注入;
不碰真实 provider,不联网。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sigma.agent.types import ToolContext
from sigma.providers.base import NeverCancelled
from sigma.team.board import ABANDON, CANCEL, Board, TeamTask, apply
from sigma.team.engine import EngineConfig, TeamEngine
from sigma.team.mailbox import Mailbox
from sigma.team.scanner import dep_tick, lease_tick
from sigma.team.store import BoardStore
from sigma.tools.builtin.multi_agent import MultiAgentParams, MultiAgentTool
from sigma.tools.builtin.team import TeamBoard

LEAD = "lead-1"


def _float_clock(start: float = 100.0) -> tuple[Any, Any]:
    state = {"now": start}

    def clock() -> float:
        return state["now"]

    def advance(delta: float) -> None:
        state["now"] += delta

    return clock, advance


def _engine(
    tmp_path: Path,
    runner: Any,
    *,
    workers: int = 1,
    goal: str = "把 X 调查清楚",
    poll_s: float = 0.02,
    lease_ttl: float = 300.0,
    clock: Any = None,
) -> tuple[TeamEngine, BoardStore, Mailbox]:
    store = BoardStore()
    mailbox = Mailbox()
    engine = TeamEngine(
        goal=goal,
        lead_id=LEAD,
        workspace_root=tmp_path,
        store=store,
        mailbox=mailbox,
        runner=runner,
        config=EngineConfig(
            workers=workers, poll_s=poll_s, lease_ttl=lease_ttl, max_rounds=4
        ),
        clock=clock,
    )
    return engine, store, mailbox


def _text(result: Any) -> str:
    assert result.content, "结果必须有 content"
    return getattr(result.content[0], "text", "")


# ---------------------------------------------------------------------------
# G-TEAM-8:Pull 语义
# ---------------------------------------------------------------------------


async def test_pull_model_workers_claim_and_finish(tmp_path: Path) -> None:
    """G-TEAM-8 的靶子:start 后引擎建板、worker 自动认领并完成,全终态收敛,
    汇总写进 lead 信箱(主 agent 下一轮 inbox 即见)。"""
    mailbox = Mailbox()
    done: list[str] = []

    async def runner(worker_id: str, description: str, max_rounds: int) -> str:
        done.append(worker_id)
        return f"结论 by {worker_id}"

    engine, store, mailbox = _engine(
        tmp_path, runner, workers=2, goal="调查 X", poll_s=0.02
    )
    summary = await asyncio.wait_for(engine.run(), timeout=5.0)
    assert "success 1" in summary
    assert done, "worker 确实执行了"
    drained = await mailbox.drain(tmp_path, LEAD)
    assert any("success" in m.text for m in drained)


async def test_stop_cancels_running_goal(tmp_path: Path) -> None:
    """G-TEAM-11 反面取证:挂起的任务被 stop 收摊 → cancelled,引擎退出。"""

    async def hang(worker_id: str, description: str, max_rounds: int) -> str:
        await asyncio.sleep(30)
        return "不可能"

    engine, store, _ = _engine(
        tmp_path, hang, goal="挂着", poll_s=0.02, lease_ttl=300.0
    )
    run_task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.1)
    assert engine.running, "worker 空转期间引擎保持运行"
    summary = await engine.stop("测试收摊")
    assert "cancelled" in summary or "撤销" in summary
    await asyncio.wait_for(run_task, timeout=2.0)


# ---------------------------------------------------------------------------
# G-TEAM-9:lease_tick(时间驱动安全网)
# ---------------------------------------------------------------------------


def test_lease_tick_reclaims_expired_running() -> None:
    """G-TEAM-9 的靶子:running 且 now > lease_deadline → 回 pending,attempts+1。"""
    board = Board()
    task = TeamTask(
        id="t1", title="挂着", state="running", assignee="w1", lease_deadline=10.0
    )
    board.tasks.append(task)
    reclaimed = lease_tick(board, now=11.0)
    assert len(reclaimed) == 1 and reclaimed[0].state == "pending"
    assert reclaimed[0].attempts == 1 and reclaimed[0].assignee == ""


def test_lease_tick_skips_alive_lease() -> None:
    """未过期的 running 不动(heartbeat 续过租)。"""
    board = Board()
    task = TeamTask(
        id="t1", title="在跑", state="running", assignee="w1", lease_deadline=110.0
    )
    board.tasks.append(task)
    assert lease_tick(board, now=11.0) == []
    assert task.state == "running"


# ---------------------------------------------------------------------------
# G-TEAM-12:双通道回收一致(worker_lost 与 lease_expired 等价)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["worker_lost", "lease_expired"])
def test_both_recovery_channels_increment_attempts(event: str) -> None:
    """G-TEAM-12 的靶子:两条回收通道对 attempts 一视同仁。"""
    board = Board()
    task = TeamTask(
        id="t1", title="在跑", state="running", assignee="w1", lease_deadline=10.0
    )
    board.tasks.append(task)
    moved = apply(board, event, task_id="t1", caller="system:engine")
    assert moved.state == "pending" and moved.attempts == 1
    assert moved.assignee == "" and moved.lease_deadline == 0.0


# ---------------------------------------------------------------------------
# G-TEAM-13:blocked / 级联 / 自动放弃(v3 新增行)
# ---------------------------------------------------------------------------


def test_dep_tick_releases_blocked_when_deps_success() -> None:
    board = Board()
    dep = TeamTask(id="t1", title="前置", state="success")
    task = TeamTask(id="t2", title="后续", state="blocked", deps=["t1"])
    board.tasks = [dep, task]
    released = dep_tick(board)
    assert len(released) == 1 and released[0].state == "pending"


def test_dep_tick_cascades_cancel_to_downstream() -> None:
    """上游 dead → 级联取消 blocked/pending 下游,迭代到不动点(多跳)。"""
    board = Board()
    a = TeamTask(id="t1", title="上游", state="fail", attempts=3)
    b = TeamTask(id="t2", title="下游", state="blocked", deps=["t1"])
    c = TeamTask(id="t3", title="下下游", state="pending", deps=["t2"])
    board.tasks = [a, b, c]
    changed = dep_tick(board)
    states = {t.id: t.state for t in board.tasks}
    assert states["t1"] == "dead", "attempts 达上限 → 自动 abandon"
    assert states["t2"] == "cancelled" and states["t3"] == "cancelled"
    assert len(changed) >= 3


def test_abandon_only_reachable_at_or_by_lead() -> None:
    """fail + abandon:lead 或扫描器可调;worker 不行(迁移表+守卫双拦);
    dead 是吸收态,任何事件再碰它都是非法迁移。"""
    board = Board()
    task = TeamTask(id="t1", title="坏了", state="fail", attempts=1)
    board.tasks.append(task)
    with pytest.raises(ValueError, match="abandon"):
        apply(board, "abandon", task_id="t1", caller="w1")
    moved = apply(board, ABANDON, task_id="t1", caller="system:scanner")
    assert moved.state == "dead"
    with pytest.raises(ValueError, match="dead"):
        apply(board, CANCEL, task_id="t1", caller=LEAD)


# ---------------------------------------------------------------------------
# G-TEAM-10:角色分权的工具面
# ---------------------------------------------------------------------------


async def test_role_faces_hide_forbidden_ops(tmp_path: Path) -> None:
    """G-TEAM-10 的靶子:lead 面无 finish/fail,worker 面无 create——
    越权 op 在工具面上不存在(返回 is_error 并说明角色),而非运行时守卫兜底。"""
    lead_tool = TeamBoard(lead_session_id=LEAD, role="lead")
    worker_tool = TeamBoard(lead_session_id=LEAD, role="worker")
    lead_ctx = ToolContext(session_id=LEAD, workspace_root=tmp_path, signal=NeverCancelled())
    worker_ctx = ToolContext(session_id="agent-a", workspace_root=tmp_path, signal=NeverCancelled())

    finish_by_lead = await lead_tool.run(
        __import__("sigma.tools.builtin.team", fromlist=["TeamBoardParams"]).TeamBoardParams(
            action="board", op="finish", id="t1", result="x"
        ),
        lead_ctx,
    )
    assert finish_by_lead.is_error is True and "无" in _text(finish_by_lead)
    create_by_worker = await worker_tool.run(
        __import__("sigma.tools.builtin.team", fromlist=["TeamBoardParams"]).TeamBoardParams(
            action="board", op="create", title="x"
        ),
        worker_ctx,
    )
    assert create_by_worker.is_error is True and "无" in _text(create_by_worker)


# ---------------------------------------------------------------------------
# G-TEAM-11:multi_agent 引擎三态(start/status/stop)
# ---------------------------------------------------------------------------


def _multi_tool(
    tmp_path: Path,
    factory: Any,
    *,
    poll_s: float = 0.02,
) -> MultiAgentTool:
    return MultiAgentTool(
        lead_session_id=LEAD,
        workspace_root=tmp_path,
        factory=factory,
        store=BoardStore(),
        mailbox=Mailbox(),
        poll_s=poll_s,
    )


async def test_multi_agent_start_status_stop(tmp_path: Path) -> None:
    """G-TEAM-11 的靶子:start 启动引擎(status 可见 running),stop 收摊
    (未终态任务 → cancelled)。factory 挂起模拟长任务。"""

    async def hang(description: str, ctx: Any, sub_id: str, max_rounds: int) -> Any:
        await asyncio.sleep(30)
        return None

    tool = _multi_tool(tmp_path, hang)
    ctx = ToolContext(session_id=LEAD, workspace_root=tmp_path, signal=NeverCancelled())
    started = await tool.run(
        MultiAgentParams(action="start", goal="调查 X", workers=1), ctx
    )
    assert started.is_error is False, _text(started)
    await asyncio.sleep(0.1)
    status = await tool.run(MultiAgentParams(action="status"), ctx)
    assert "运行中" in _text(status)
    stopped = await tool.run(
        MultiAgentParams(action="stop", reason="测试收摊"), ctx
    )
    assert stopped.is_error is False
    assert "cancelled" in _text(stopped) or "撤销" in _text(stopped)
    await asyncio.sleep(0.05)
    assert not tool.engine_running


async def test_multi_agent_start_requires_goal(tmp_path: Path) -> None:
    tool = MultiAgentTool(
        lead_session_id=LEAD,
        workspace_root=tmp_path,
        factory=None,
        store=BoardStore(),
        mailbox=Mailbox(),
    )
    ctx = ToolContext(session_id=LEAD, workspace_root=tmp_path, signal=NeverCancelled())
    result = await tool.run(MultiAgentParams(action="start", goal="  "), ctx)
    assert result.is_error is True
