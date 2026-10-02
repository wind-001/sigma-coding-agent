"""multi_agent 引擎与 Scanner 的门禁测试(G-TEAM-8..16,详规 v3 §6)。

全部离线:引擎的 runner 用假实现(立即返回结论或挂起),clock 可注入;
不碰真实 provider,不联网。

G-TEAM-14(装配级角色面)在 test_team_board.py——它要的是真实装配,
本文件只测引擎与门面。
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
from sigma.team.lead_board import LeadBoard
from sigma.team.mailbox import Mailbox
from sigma.team.scanner import dep_tick, lease_tick
from sigma.team.store import BoardStore
from sigma.team.worker_board import WorkerBoard
from sigma.tools.builtin.multi_agent import MultiAgentParams, MultiAgentTool
from sigma.tools.builtin.team import TeamBoard, TeamBoardParams

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


async def test_stop_writes_exactly_one_summary(tmp_path: Path) -> None:
    """B2 的靶子:stop 之后 lead 信箱**恰好一封**汇总,且不是"自动收摊"那封。

    修复前:stop() 写一封"引擎停止:…",run() 随后又写一封"全部任务已达终态,
    团队自动收摊"——后一封与事实相反(任务是被撤销的,不是完成的),而主 agent
    只看到"有新消息",读到的偏偏是错的那封。
    """
    async def hang(worker_id: str, description: str, max_rounds: int) -> str:
        await asyncio.sleep(30)
        return "不可能"

    engine, _store, mailbox = _engine(tmp_path, hang, goal="挂着", poll_s=0.02)
    run_task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.1)
    await engine.stop("测试收摊")
    await asyncio.wait_for(run_task, timeout=2.0)

    drained = await mailbox.drain(tmp_path, LEAD)
    assert len(drained) == 1, [m.text for m in drained]
    assert "测试收摊" in drained[0].text, drained[0].text
    assert "自动收摊" not in drained[0].text, (
        "stop 场景不该收到「已达终态/自动收摊」的措辞——那是 B2 的旧缺陷"
    )


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


# ---------------------------------------------------------------------------
# G-TEAM-15:幂等收尾(①-c 的必需部件)
# ---------------------------------------------------------------------------


async def test_g_team15_try_conclude_is_idempotent(tmp_path: Path) -> None:
    """G-TEAM-15 的靶子(门面级):任务已被 worker 自己收尾时,幂等收尾返回 None。

    为什么必须有它:①-c 把 **worker 面**下发给了子会话,worker 从此能自己
    finish/fail;引擎若仍无条件收尾,``fail+FINISH`` 撞非法迁移,异常又被
    except 分支放大成第二次(``fail+FAIL``),协程带异常结束,而任务停在
    **非终态的 fail** 上 ⇒ 引擎永不收敛。
    """
    store = BoardStore(clock=lambda: "2026-09-30T10:00:00.000")
    lead = LeadBoard(store, LEAD, tmp_path)
    worker = WorkerBoard(store, "worker-0", tmp_path, clock=lambda: 10.0)
    await lead.create("调查 X")
    claimed = await worker.try_claim()
    assert claimed is not None
    await worker.fail(claimed.id, "搞不定")

    # 引擎随后收尾:跳过,不抛
    assert await worker.try_finish(claimed.id, "引擎的结论") is None
    assert await worker.try_fail(claimed.id, "引擎的异常") is None

    board = store.load(tmp_path)
    assert board is not None
    assert board.tasks[0].state == "fail", "幂等收尾不得把状态改回去"
    assert board.tasks[0].attempts == 1, "幂等收尾不得再抬 attempts"
    assert board.tasks[0].result == "搞不定", "幂等收尾不得覆盖 worker 的结论"

    # 幂等 != 不干活:还在 running 的任务照常收
    await lead.create("第二条")
    second = await worker.try_claim()
    assert second is not None
    done = await worker.try_finish(second.id, "正常结论")
    assert done is not None and done.state == "success"


async def test_g_team15_engine_skips_when_worker_concluded(tmp_path: Path) -> None:
    """G-TEAM-15 的靶子(引擎级):worker 自己 finish 后,引擎不重复收尾。

    可证伪点:修复前引擎会 ``wb.finish`` → 撞非法迁移 → except 里再 ``wb.fail``
    → 再撞 → **worker 协程带着 ValueError 死掉**(异常无人回收)。所以除了断言
    结论没被覆盖,还要断言 **worker 协程无异常**——这是把修复前后分开的观测点。
    """
    store = BoardStore()
    mailbox = Mailbox()

    async def runner(worker_id: str, description: str, max_rounds: int) -> str:
        # 模拟子 agent 自己收尾:走 **worker 面**工具,身份 = worker_id
        # (引擎把 worker_id 直接用作子会话 id,assignee 与 ctx.session_id 一致)
        tool = TeamBoard(
            lead_session_id=LEAD, role="worker", store=store, mailbox=mailbox
        )
        board = store.load(tmp_path)
        assert board is not None
        mine = [task for task in board.tasks if task.assignee == worker_id]
        assert mine, "runner 应当看到被自己认领的任务"
        finished = await tool.run(
            TeamBoardParams(action="board", op="finish", id=mine[0].id, result="自收尾"),
            ToolContext(
                session_id=worker_id, workspace_root=tmp_path, signal=NeverCancelled()
            ),
        )
        assert finished.is_error is False, _text(finished)
        return "引擎这边也会拿到一个结论(不该覆盖上面那一条)"

    engine = TeamEngine(
        goal="调查 X",
        lead_id=LEAD,
        workspace_root=tmp_path,
        store=store,
        mailbox=mailbox,
        runner=runner,
        config=EngineConfig(workers=1, poll_s=0.02, lease_ttl=300.0, max_rounds=4),
    )
    summary = await asyncio.wait_for(engine.run(), timeout=5.0)
    assert "success 1" in summary
    board = store.load(tmp_path)
    assert board is not None
    assert board.tasks[0].result == "自收尾", board.tasks[0].result

    # 等 worker 协程自行退出(收敛后它 return;给 poll_s 留点余量)
    for _ in range(100):
        if all(task.done() for task in engine._tasks):
            break
        await asyncio.sleep(0.02)
    for worker_task in engine._tasks:
        assert worker_task.done(), "worker 应当已退出"
        assert worker_task.exception() is None, worker_task.exception()


# ---------------------------------------------------------------------------
# G-TEAM-16:失败必须让 lead 看见(②-b)
# ---------------------------------------------------------------------------


async def test_g_team16_failed_task_notifies_lead_exactly_once(tmp_path: Path) -> None:
    """G-TEAM-16 的靶子(②-b):失败必须让 lead 看见,且同一次失败只看见一封。

    为什么非做不可:fail **不是终态**(收敛判据要求全终态),而重派权归 lead。
    "失败 + lead 不知情" = 引擎与 worker 永久空转、汇总永远不写。

    顺带钉住同族的第二个缺陷:**板上存在 fail 任务时 stop() 不能崩**——fail 是
    "非终态但迁移表里没有 cancel 出边",曾经 stop 无条件 cancel 会抛非法迁移,
    想收摊反而收不掉。
    """

    async def boom(worker_id: str, description: str, max_rounds: int) -> str:
        raise RuntimeError("演练:worker 崩了")

    engine, store, mailbox = _engine(
        tmp_path, boom, workers=1, goal="调查 X", poll_s=0.02
    )
    run_task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.3)  # 让它 poll 十几轮:去重必须扛得住

    board = store.load(tmp_path)
    assert board is not None and board.tasks[0].state == "fail"

    summary = await engine.stop("测试收摊")  # fail 不收敛,只能人为收摊
    assert "非法迁移" not in summary, summary
    await asyncio.wait_for(run_task, timeout=2.0)

    drained = await mailbox.drain(tmp_path, LEAD)
    notices = [message for message in drained if "失败待决" in message.text]
    assert len(notices) == 1, [message.text for message in notices]
    assert "attempts=1" in notices[0].text, notices[0].text
    assert "RuntimeError" in notices[0].text, notices[0].text


# ---------------------------------------------------------------------------
# D1 补强:心跳必须**真跑**(Review-2026-09-30 §5)
# ---------------------------------------------------------------------------


async def test_worker_heartbeat_really_extends_lease(tmp_path: Path) -> None:
    """D1 的靶子:真调一次 :meth:`WorkerBoard.heartbeat`,租约被**实测**续上。

    为什么原 G-TEAM-9 不够
        那条用例是**手工把 ``lease_deadline`` 设成未来**来模拟"续过租"
        (``test_lease_tick_skips_alive_lease``)——从没经过 ``heartbeat()``。
        于是 ``WorkerBoard.heartbeat`` 与 ``engine._heartbeat_loop`` 零覆盖:
        **把这两段整块删掉,测试仍然全绿**。

    注入变红:让 ``heartbeat`` 变成 no-op(或删掉)——租约不再前进,断言红。
    """
    now = {"t": 100.0}

    def clock() -> float:
        return now["t"]

    store = BoardStore()
    lead = LeadBoard(store, LEAD, tmp_path)
    worker = WorkerBoard(store, "w1", tmp_path, lease_ttl=10.0, clock=clock)
    await lead.create("长任务")
    claimed = await worker.try_claim()
    assert claimed is not None
    assert claimed.lease_deadline == 110.0, "claim 写租约:now(100) + ttl(10)"

    now["t"] = 108.0
    refreshed = await worker.heartbeat("t1")
    assert refreshed.lease_deadline == 118.0, (
        "heartbeat 必须按**当前时刻**重续租约(108 + 10);"
        "若它退化成 no-op,这条当场红"
    )


async def test_engine_heartbeat_keeps_long_task_alive(tmp_path: Path) -> None:
    """D1 的引擎侧:任务跑得比 lease TTL 久,靠 ``_heartbeat_loop`` 不被回收。

    注入变红:删掉 ``engine._heartbeat_loop``(或不再 create_task 它)。

    ⚠ 这条测试曾是**假门**:实测 3 次里飘 1 次,而它的 docstring 声称
    「删掉心跳会确定性变红」——**不稳定 = 不能证伪**。

    飘的原因**有两个**,别只盯着一个(我第一版也只写下了第一个,后来被自己
    的实测推翻,已改正):

    ① 时序余量被压到定时器抖动量级(2026-10-02 已修)
       名义心跳周期 = ttl/3 = 16.67 ms,每轮 lease 余量就是 16.7 ms;
       实测 asyncio.sleep 落点正偏差 50.7 / 76.9 / 55.3 / 61.2 / 56.5 ms,
       **是余量的 3–5 倍**(Windows 定时器粒度约 15.6 ms,再叠加
       ``transaction`` 里的同步写盘与事件循环排队)。
       ⇒ 放大到 ttl=1.0s(余量 333 ms = 抖动的 4.3 倍),
       slow 的 2.5s 仍是 ttl 的 2.5 倍,判别力不变。代价 0.3s → 2.5s。

    ② **IO 故障杀死心跳**(真缺陷,2026-10-02 已修,见
       ``TeamEngine._heartbeat_loop`` 与 ``store._replace_with_retry``)
       Windows 上 ``os.replace`` 偶发 ``PermissionError: [WinError 5]``,
       40 轮引擎实跑(约 1000 次写盘)命中 1 次。旧实现的
       ``_heartbeat_loop`` 只 ``except ValueError`` ⇒ 协程带异常猝死 ⇒
       租约不再续 ⇒ 任务被误回收(attempts=1,state 仍 success——因为重派
       后跑成功了,所以**只看 state 会漏判**,必须断言 attempts)。
       顺手也修了 ``_worker_loop`` 与 ``_wait_converged`` 同样的猝死。

    判别力守则:改完必须重验注入(删 ``_heartbeat_loop`` → 必须**确定性**红)。
    只看到"全绿"不算数——那正是本条修复前的样子。

    ⚠ 注入后的失败形态是 ``TimeoutError`` 而**不是** ``attempts==1``,
    这不是判别力缺失,但必须写下来以免后人误判(2026-10-02 实测):
    完全无心跳时任务被**反复**回收重派,而 ``scanner`` 的自动放弃只管
    ``state == "fail"``(scanner.py:86),**lease 回收把任务打回 pending
    ⇒ 绕过重试上限** ⇒ 实测 90 s 仍在重派、attempts 涨到 36、永不收敛。
    所以"删心跳"必然以超时收场,而不是撞上下面那两条断言。

    ⇒ 两条断言守的其实是**另一个**场景:**部分**心跳失败(IO 抖动那一次)
    后任务被回收一次、重派后跑成功 ⇒ state=success 而 attempts=1。
    那才是只看 state 会漏判、必须断言 attempts 的地方。
    （那个"lease 回收绕过重试上限"的缺陷已单列,不在本条修复范围内。）
    """

    async def slow(worker_id: str, description: str, max_rounds: int) -> str:
        await asyncio.sleep(2.5)  # 远长于 lease_ttl=1.0
        return "干完了"

    engine, store, _mailbox = _engine(
        tmp_path, slow, goal="慢活", poll_s=0.02, lease_ttl=1.0
    )
    await asyncio.wait_for(engine.run(), timeout=15.0)
    board = store.load(tmp_path)
    assert board is not None
    task = board.find("t1")
    assert task is not None
    assert task.state == "success", f"心跳没续上租约,任务被误回收:{task.state}"
    assert task.attempts == 0, "被误回收过的话 attempts 会 > 0"


# ---------------------------------------------------------------------------
# G99：IO 韧性（2026-10-02）——一次写盘抖动不许杀死任何引擎协程
# ---------------------------------------------------------------------------
#
# 起因是一条**实测**的故障链，不是假想：
#   Windows 上 os.replace 偶发 PermissionError: [WinError 5] 拒绝访问
#   （杀软/索引服务/EPERM 竞争）——40 轮引擎实跑约 1000 次写盘命中 1 次。
#   修复前的后果是**静默**的：
#     ① _heartbeat_loop 只 except ValueError ⇒ 协程猝死 ⇒ 租约不续
#        ⇒ 任务被误回收 ⇒ attempts 累积（state 仍是 success，重派后跑成功了，
#        **只看 state 会漏判**）
#     ② _worker_loop 猝死 ⇒ 任务永远卡 pending，run() 挂到超时
#     ③ _wait_converged 猝死 ⇒ 没人再看板有没有非终态任务 ⇒ 永不收敛
#        （全量测试里那条 store.py:86 PermissionError 就是从这里出去的）
#
# 这三条的共同形状：**保活机制被一次瞬时 IO 抖动打断，且无人知晓**。
# 门的作用是钉死"抖动被吸收"这个行为，注入点是 monkeypatch _save / heartbeat。


class TestIoResilience:
    """每条门都报分母（检查了 N 次调用），N==0 一律按失败论。"""

    @staticmethod
    def _flaky(
        monkeypatch: pytest.MonkeyPatch, target: Any, name: str, *, fail_times: int
    ) -> list[str]:
        """把 ``target.name`` 改成"前 fail_times 次抛 OSError,之后正常"。

        返回调用记录列表——门必须能断言"确实被调用过 N 次"，
        否则一个"从没被触发"的注入会伪装成通过。
        """
        real = getattr(target, name)
        calls: list[str] = []

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            calls.append("x")
            if len(calls) <= fail_times:
                raise PermissionError(5, "拒绝访问")
            return real(*args, **kwargs)

        monkeypatch.setattr(target, name, wrapper)
        return calls

    @staticmethod
    def _fail_after(
        monkeypatch: pytest.MonkeyPatch, *, skip: int, fail_times: int
    ) -> list[str]:
        """让 ``BoardStore._save`` 跳过前 ``skip`` 次正常，之后失败 ``fail_times`` 次。

        ⚠ ``skip`` 不是凑的，它对应真实时序：``run()`` 的第一步是
        ``LeadBoard.create``，那是**第 1 次**事务。持续写盘故障下引擎起不来
        是正确行为（没有板就没法跑），所以 B 层要守的场景是
        「**引擎已经跑起来之后**磁盘抖了一下」——注入必须落在那之后。

        第一版门没做 skip，直接从第 1 次就失败，结果 ``run()`` 冒泡、
        门红。那不是缺陷，是**门建模错了故障时序**。教训与压缩轮那条同源：
        门的注入点 = 缺陷的实际起点，起点错了就是把别的缺陷测成红的。

        ⚠ 另一个坑（第一版也踩了）：不能注入 ``os.replace``——那打到的是
        A 层（``_replace_with_retry``），它会把瞬时失败吃进重试预算 ⇒
        事务其实成功 ⇒ B 层一步没走 ⇒ 门绿而虚。所以注入在事务层。
        """
        real = BoardStore._save
        calls: list[str] = []

        def wrapper(self: Any, root: Path, board: Any) -> Any:
            calls.append("x")
            if len(calls) > skip and len(calls) <= skip + fail_times:
                raise PermissionError(5, "拒绝访问")
            return real(self, root, board)

        monkeypatch.setattr(BoardStore, "_save", wrapper)
        return calls

    def test_save_retries_transient_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 层：_save 的 os.replace 前两次失败必须被重试吃掉,且落盘内容正确。

        注入变红：把 _replace_with_retry 换回裸 os.replace ⇒ 前两次
        PermissionError 直接冒泡 ⇒ 本条红。
        """
        import sigma.team.store as store_mod

        calls = self._flaky(
            monkeypatch, store_mod.os, "replace", fail_times=2
        )
        store = BoardStore()
        board = Board()
        board.tasks.append(TeamTask(id="t1", title="活", state="running"))
        store._save(tmp_path, board)

        assert len(calls) == 3, f"应重试到第 3 次才成功,实际调了 {len(calls)} 次"
        reloaded = store.load(tmp_path)
        assert reloaded is not None and reloaded.find("t1") is not None, (
            "重试成功但内容没落对——重试掩盖了真实故障"
        )

    def test_save_reraises_after_budget_exhausted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 层的另一半：重试**不能无限重试**。

        真失败（磁盘满/只读目录）必须照旧冒泡，否则每个事务都卡 155 ms，
        而"立刻报错"才是正确行为。断言异常类型与 errno 原样透出——
        包一层自己的异常会让上层所有 except 失配。
        """
        import sigma.team.store as store_mod

        calls = self._flaky(
            monkeypatch, store_mod.os, "replace", fail_times=99
        )
        store = BoardStore()
        with pytest.raises(PermissionError) as excinfo:
            store._save(tmp_path, Board())
        assert len(calls) == store_mod.REPLACE_ATTEMPTS, (
            f"应恰好重试 {store_mod.REPLACE_ATTEMPTS} 次,"
            f"实际 {len(calls)} 次（多了=拖慢真失败,少了=放弃太早）"
        )
        assert excinfo.value.errno == 5, "异常被包装过,errno 丢了"

    @pytest.mark.asyncio
    async def test_heartbeat_survives_transient_io_fault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B 层：心跳遇 OSError 必须**继续续**，不能退出。

        注入变红：_heartbeat_loop 回到"只 except ValueError" ⇒ 前两次
        PermissionError 冒泡 ⇒ 本条红。
        """

        async def _never(worker_id: str, description: str, rounds: int) -> str:
            return "不会被调用"  # 本条直接调 _heartbeat_loop,不经 runner
        engine, store, _ = _engine(tmp_path, _never, lease_ttl=0.3)
        board = store.load(tmp_path) or Board()
        board.tasks.append(
            TeamTask(id="t1", title="慢活", state="running", assignee="w1",
                     lease_deadline=1e9)
        )
        store._save(tmp_path, board)
        wb = WorkerBoard(store, "w1", tmp_path, lease_ttl=0.3)

        # 心跳第 1、2 次抛 OSError；第 3 次抛 ValueError（任务已不在 running）
        # ——后者用来**干净地终止循环**，于是本条能同时证明两件事：
        # 异常被吞了（没冒泡）且循环确实走到了第 3 次（没提前 return）。
        real_hb = WorkerBoard.heartbeat
        seen: list[str] = []

        async def flaky(self: Any, task_id: str) -> Any:
            seen.append(task_id)
            if len(seen) <= 2:
                raise PermissionError(5, "拒绝访问")
            raise ValueError("任务已不在 running")

        monkeypatch.setattr(WorkerBoard, "heartbeat", flaky)
        await asyncio.wait_for(engine._heartbeat_loop(wb, "t1"), timeout=5.0)

        assert len(seen) == 3, f"心跳只走了 {len(seen)} 次就该被前两次异常打断"
        assert engine._io_faults == 1, (
            f"IO 故障被吞了但没计数（{engine._io_faults}）——"
            "又一次静默丢弃，违背『丢弃必须可见』"
        )

    @pytest.mark.asyncio
    async def test_worker_loop_survives_io_fault_and_task_still_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B 层最严重的一处：worker 猝死 ⇒ 任务永远卡 pending。

        注入变红：_worker_loop 去掉外层 except OSError ⇒ 首次写盘失败即猝死
        ⇒ run() 挂死 ⇒ 本条在 wait_for 上红（TimeoutError）。

        断言 attempts==0 是刻意的：worker 被打断后若发生**误回收**，
        重派会跑成功、state 仍是 success，只看 state 会漏判（真踩过）。
        """
        import sigma.team.store as store_mod

        async def slow(worker_id: str, description: str, max_rounds: int) -> str:
            await asyncio.sleep(0.2)
            return "干完了"

        engine, store, _ = _engine(
            tmp_path, slow, goal="慢活", poll_s=0.02, lease_ttl=0.5
        )
        # 前 3 次**事务**失败：足够打到 tick 事务与 try_claim 两处猝死点。
        calls = self._fail_after(monkeypatch, skip=1, fail_times=3)

        await asyncio.wait_for(engine.run(), timeout=20.0)
        board = store.load(tmp_path)
        assert board is not None
        task = board.find("t1")
        assert task is not None
        assert len(calls) > 5, f"注入没被触发（只调了 {len(calls)} 次），门是空跑"
        assert task.state == "success", f"worker 被打断后任务没收尾:{task.state}"
        assert task.attempts == 0, "worker 被打断期间发生了误回收"

    @pytest.mark.asyncio
    async def test_convergence_supervisor_survives_io_fault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B 层第三处：监督协程猝死 ⇒ 永不收敛（界面只显示"运行中"）。

        这条正是全量测试里 store.py:86 PermissionError 的出处。
        注入变红：_wait_converged 去掉 except OSError ⇒ run() 冒泡 ⇒ 红。
        """
        async def slow(worker_id: str, description: str, max_rounds: int) -> str:
            await asyncio.sleep(0.1)
            return "干完了"

        engine, store, _ = _engine(
            tmp_path, slow, goal="活", poll_s=0.02, lease_ttl=0.5
        )
        calls = self._fail_after(monkeypatch, skip=1, fail_times=4)

        summary = await asyncio.wait_for(engine.run(), timeout=20.0)
        assert len(calls) > 5, f"注入没被触发（只调了 {len(calls)} 次），门是空跑"
        assert "全部任务已达终态" in summary, (
            f"监督协程被打断后没收敛，返回的是：{summary!r}"
        )
        assert engine._io_faults > 0, "IO 故障被吸收了却没计数"

    @pytest.mark.asyncio
    async def test_io_fault_alert_goes_to_lead_mailbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """『丢弃必须可见』：故障必须**送达 lead 信箱**，不只是内部计数。

        项目全局不装 logging，lead 信箱是 team 层唯一的用户可见通道。
        两个方向都要验：
        ①**同一故障点不刷屏**——worker-0 会被打中好几次，只该发 1 封;
        ②**不同故障点不合并**——worker 与监督协程是两处独立故障点,
          合并成一封就等于把其中一个藏起来。

        ⚠ 我第一版把期望写成"恰好 1 封"，那是**错的期望**：
        去重的粒度是**故障点**，不是全局。两处故障就该两封信。
        """
        async def slow(worker_id: str, description: str, max_rounds: int) -> str:
            await asyncio.sleep(0.1)
            return "干完了"

        engine, store, mailbox = _engine(
            tmp_path, slow, goal="活", poll_s=0.02, lease_ttl=0.5
        )
        calls = self._fail_after(monkeypatch, skip=1, fail_times=4)
        await asyncio.wait_for(engine.run(), timeout=20.0)

        assert len(calls) > 5, f"注入没被触发（只调了 {len(calls)} 次），门是空跑"
        msgs = await mailbox.drain(tmp_path, LEAD)
        alerts = [m for m in msgs if "IO 故障" in m.text]
        assert alerts, "一封都没有——故障被静默丢弃了"

        # 文案形如 "[IO 故障] <where>: <ExcType>: ..."，<where> 即去重键
        where_of = [m.text.split("] ", 1)[1].split(":")[0] for m in alerts]
        assert len(where_of) == len(set(where_of)), (
            f"同一故障点发了多封，在刷屏：{where_of}"
        )
        assert store.load(tmp_path) is not None


# ---------------------------------------------------------------------------
# D3 补强:异常路径必须**经引擎**取证
# ---------------------------------------------------------------------------


async def test_engine_failure_path_records_fail_and_notifies(tmp_path: Path) -> None:
    """D3 的靶子:runner 抛异常 → 引擎记 ``fail``(attempts+1)+ ②-b 通知 lead。

    为什么纯函数参数化不够
        G-TEAM-12/13 只喂 ``board.apply``,恰好绕过真实分歧点:引擎的 except
        分支怎么写、fail 之后谁去通知。这条把两件事一起钉住。

    注入变红:去掉 except 里的 ``try_fail`` → 任务停在 running;去掉通知 →
    信箱里没有待决提醒。
    """

    async def boom(worker_id: str, description: str, max_rounds: int) -> str:
        raise RuntimeError("工具炸了")

    engine, store, mailbox = _engine(tmp_path, boom, goal="注定失败", poll_s=0.02)
    run_task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.2)

    board = store.load(tmp_path)
    assert board is not None
    task = board.find("t1")
    assert task is not None
    assert task.state == "fail", "异常必须落 fail,而不是停在 running"
    assert task.attempts == 1, "fail 一次 attempts 记 1"
    assert "RuntimeError" in task.result, "失败原因要落进 result(重派/放弃都要用)"

    drained = await mailbox.drain(tmp_path, LEAD)
    assert any("失败待决" in message.text for message in drained), (
        "②-b:fail 不是终态,lead 必须被通知到,否则团队永久空转"
    )

    await engine.stop("测试收尾")
    await asyncio.wait_for(run_task, timeout=2.0)


# ---------------------------------------------------------------------------
# D4 补强:空转不退出
# ---------------------------------------------------------------------------


async def test_idle_workers_keep_polling(tmp_path: Path) -> None:
    """D4 的靶子:板上无活可认领时 worker **空转不退出**,且新任务进板即被认领。

    上半句(v1 从未有用例):把 ``_worker_loop`` 里 ``try_claim() is None`` 的
    分支改成 ``return`` → 空闲 worker 协程立刻结束,断言红。
    下半句(空转不是罢工):新任务落板后必须有人在下一轮 poll 认领它。
    """

    async def hang(worker_id: str, description: str, max_rounds: int) -> str:
        await asyncio.sleep(30)
        return "不可能"

    engine, store, _mailbox = _engine(
        tmp_path, hang, workers=2, goal="占住一个 worker", poll_s=0.02
    )
    run_task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.15)

    assert all(not worker_task.done() for worker_task in engine._tasks), (
        "有空闲 worker 时,它必须还在轮询(空转不是退出)"
    )

    lead = LeadBoard(store, LEAD, tmp_path)
    await lead.create("新活")
    await asyncio.sleep(0.15)
    board = store.load(tmp_path)
    assert board is not None
    fresh = board.find("t2")
    assert fresh is not None and fresh.state == "running", (
        "空转的 worker 应当在下一轮 poll 认领新任务"
    )

    await engine.stop("测试收尾")
    await asyncio.wait_for(run_task, timeout=2.0)
