"""multi_agent 引擎:worker 常驻循环 + Scanner 轮询 + 自动收敛(Pull 模型,v3)。

角色边界(详规 §2,星辰设计评审)
    引擎**不是 agent**:它每轮 poll 先代调 Scanner 两个 tick(逻辑独立角色,
    物理是几行轮询),再让 worker 门面从 pending 捞活(claim)。worker 的
    实际执行经注入的 ``runner``(sdk 组装的隔离子会话,与 TaskTool 同一
    工厂家族);LLM 永远不需要知道 lease 的存在——心跳由引擎为 running
    任务自动续(每 poll 刷新 lease_deadline)。

自动收敛
    板上全部任务进终态(success/dead/cancelled)且至少存在过一个任务 →
    引擎停worker、写汇总进 lead 信箱——主 agent 下一轮 inbox 即见,
    不需要手动收摊。

生命周期
    ``run()`` 是引擎主协程(start 动作经 ``asyncio.create_task`` 后台跑);
    ``stop()`` 取消 worker、按 lead 身份 cancel 非终态任务;会话事件循环
    终止时后台任务随之消亡,未完任务由下次 start 的 lease_tick 回收
    (安全网;主路径是 worker 主动 finish/fail)。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sigma.team.board import ABANDON, CANCEL, TeamTask, apply
from sigma.team.lead_board import LeadBoard
from sigma.team.mailbox import Mailbox
from sigma.team.scanner import dep_tick, lease_tick
from sigma.team.store import BoardStore
from sigma.team.worker_board import WorkerBoard

#: runner 签名:(worker_id, description, max_rounds) → 子会话结论文本。
#: 异常上抛 = worker_lost 语义(引擎转 fail,attempts+1)。
Runner = Callable[[str, str, int], Awaitable[str]]


@dataclass(frozen=True)
class EngineConfig:
    workers: int = 2
    poll_s: float = 2.0
    lease_ttl: float = 300.0
    max_rounds: int = 16


_TERMINAL: frozenset[str] = frozenset({"success", "dead", "cancelled"})


class TeamEngine:
    """一块板 + N 个常驻 worker + Scanner 轮询的引擎。一次 start 一个实例。"""

    def __init__(
        self,
        *,
        goal: str,
        lead_id: str,
        workspace_root: Path,
        store: BoardStore,
        mailbox: Mailbox,
        runner: Runner,
        config: EngineConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._goal = goal.strip()
        self._lead_id = lead_id
        self._root = workspace_root
        self._store = store
        self._mailbox = mailbox
        self._runner = runner
        self._config = config or EngineConfig()
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._lead = LeadBoard(store, lead_id, workspace_root)
        self._stopping = False
        self._converged = False
        self._tasks: list[asyncio.Task[None]] = []  # worker/监督协程
        self._started_at: float | None = None
        # ②-b:已通知过 lead 的失败。键 = (任务 id, attempts)——同一次失败被每轮
        # poll 反复看到不刷屏;重派后再失败会形成新键,**再通知一次**(那是新信息)。
        self._notified_failures: set[tuple[str, int]] = set()

    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    async def run(self) -> str:
        """引擎主协程:建初始任务 → spawn worker → 等收敛 → 写汇总。"""
        self._started_at = self._clock()
        await self._lead.create(self._goal)
        self._tasks = [
            asyncio.create_task(self._worker_loop(f"worker-{i}"))
            for i in range(self._config.workers)
        ]
        await self._wait_converged()
        if self._stopping:
            # stop() 已经写过那一封汇总。B2 之前这里会再写一封,且措辞
            # "全部任务已达终态,团队自动收摊"与事实相反——任务是被撤销的,
            # 不是完成的;而主 agent 只看到"有新消息",读到的是错的那封。
            return ""
        summary = await self._summarize("全部任务已达终态,团队自动收摊")
        return summary

    async def stop(self, reason: str) -> str:
        """人为收摊:非终态任务按 lead 身份 cancel(级联由扫描器补齐)。"""
        self._stopping = True
        async with self._store.transaction(self._root) as board:
            for task in board.tasks:
                if task.state in _TERMINAL:
                    continue
                if task.state == "fail":
                    # fail **不是终态**,可迁移表里没有 ``fail+cancel`` 这一行
                    # (fail 的出边只有 reclaim→pending / abandon→dead)。
                    # 曾经这里无条件 cancel ⇒ "板上只要有一个失败任务,stop 就抛
                    # 非法迁移"——想收摊反而收不掉。
                    apply(
                        board,
                        ABANDON,
                        task_id=task.id,
                        caller=self._lead_id,
                        lead=self._lead_id,
                        note=reason or "引擎 stop",
                    )
                    continue
                apply(
                    board,
                    CANCEL,
                    task_id=task.id,
                    caller=self._lead_id,
                    lead=self._lead_id,
                    note=reason or "引擎 stop",
                )
        for worker_task in self._tasks:
            worker_task.cancel()
        # 必须 await:`cancel()` 只是"请求",不等它落定就写汇总,汇总里可能
        # 还带着 worker 没收尾的中间态;而且裸调用 gather 会留下一个**永不
        # 被 await 的协程**(B1:评审实测的悬空协程 + 退出时序无保证)。
        await asyncio.gather(*self._tasks, return_exceptions=True)
        return await self._summarize(f"引擎停止:{reason or '未说明'}")

    def snapshot(self) -> str:
        """status 视图:板渲染 + 引擎运行态(同步读,不落盘)。"""
        board = self._store.load(self._root)
        lines = [f"引擎:{'运行中' if self.running else '已停止'}"]
        if board is not None:
            lines.append(board.render())
        else:
            lines.append("任务板还是空的。")
        return "\n".join(lines)

    # ------------------------------------------------------------------

    async def _wait_converged(self) -> None:
        while not self._stopping:
            async with self._store.transaction(self._root) as board:
                dep_tick(board)
                lease_tick(board, now=self._clock())
                tasks = board.tasks
                converged = bool(tasks) and all(
                    task.state in _TERMINAL for task in tasks
                )
                fresh_failures = self._unnotified_failures(tasks)
            # 信箱 I/O 放事务**外**:持着板锁做文件 I/O 没必要,也会把 claim 卡住。
            if fresh_failures:
                await self._notify_failures(fresh_failures)
            if converged:
                self._converged = True
                return
            await asyncio.sleep(self._config.poll_s)

    def _unnotified_failures(self, tasks: Sequence[TeamTask]) -> list[TeamTask]:
        """挑出**还没通知过 lead** 的失败任务(去重键 = 任务 id + attempts)。

        键里带 attempts:同一任务被重派后再失败要**再通知一次**(那是新信息),
        而同一次失败被每轮 poll 反复看到,不该刷屏。
        """
        return [
            task
            for task in tasks
            if task.state == "fail"
            and (task.id, task.attempts) not in self._notified_failures
        ]

    async def _notify_failures(self, tasks: Sequence[TeamTask]) -> None:
        """②-b 的兜底:**失败必须让 lead 看见**。

        为什么非做不可
            ``fail`` **不是终态**(收敛判据要求全终态),而重派权按详规 §5 归 lead。
            于是"任务失败 + lead 不知情"= 引擎与 worker 永久空转、汇总永远不写、
            ``multi_agent`` 永远显示"运行中"——**不是记错了,是没人知道**。

        为什么放在 poll,而不是引擎的 except 分支
            ①-c 之后 worker 能自己调 fail,那条路径不经过引擎的 except;
            只有 poll 能同时覆盖"引擎代记的失败"与"worker 自报的失败"。
        """
        for task in tasks:
            self._notified_failures.add((task.id, task.attempts))
            await self._mailbox.send(
                self._root,
                to=self._lead_id,
                sender="system:engine",
                text=(
                    f"[失败待决] {task.id}《{task.title}》attempts={task.attempts}"
                    f" — {task.result or '(无原因)'}。"
                    "请 reclaim 重派 或 abandon 放弃;你不处理,团队不会收敛。"
                ),
            )

    async def _worker_loop(self, worker_id: str) -> None:
        wb = WorkerBoard(
            self._store,
            worker_id,
            self._root,
            lease_ttl=self._config.lease_ttl,
            clock=self._clock,
        )
        while not self._stopping:
            async with self._store.transaction(self._root) as board:
                dep_tick(board)
                lease_tick(board, now=self._clock())
            claimed = await wb.try_claim()
            if claimed is None:
                if self._converged:
                    return
                await asyncio.sleep(self._config.poll_s)
                continue
            description = (
                f"团队任务 {claimed.id}:{claimed.title}。"
                "这是共享任务板上分配给你的独立任务。"
                "正常完成:直接回结论文本,引擎替你登记;"
                "搞不定:用 team_board 的 board op=fail 写明原因"
                "(装死要等 lease 超时才被回收,不划算)。"
            )
            lease = asyncio.create_task(self._heartbeat_loop(wb, claimed.id))
            try:
                result = await self._runner(
                    worker_id, description, self._config.max_rounds
                )
                # 幂等收尾:子 agent 拿到 worker 面(①-c)之后可以自己 finish/fail,
                # 那时这里必须**跳过**而不是再收一次——重复收尾撞非法迁移,
                # 异常还会被 except 分支放大成第二次,协程带异常死掉。
                await wb.try_finish(claimed.id, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # worker_lost 的事件驱动通道:异常即认输
                await wb.try_fail(claimed.id, f"{type(exc).__name__}: {exc}")
            finally:
                lease.cancel()

    async def _heartbeat_loop(self, wb: WorkerBoard, task_id: str) -> None:
        """lease 自动续租:worker 存活期间引擎每 TTL/3 续一次——LLM 不可见。"""
        ttl = self._config.lease_ttl
        while True:
            await asyncio.sleep(ttl / 3)
            try:
                await wb.heartbeat(task_id)
            except ValueError:
                return  # 任务已不在 running(被回收/取消)——心跳自然结束

    async def _summarize(self, headline: str) -> str:
        board = self._store.load(self._root)
        body = board.render() if board is not None else "(板为空)"
        summary = f"{headline}\n{body}"
        await self._mailbox.send(
            self._root, to=self._lead_id, sender="system:engine", text=summary
        )
        return summary
