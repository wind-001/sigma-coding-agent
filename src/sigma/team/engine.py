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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from sigma.team.board import CANCEL, apply
from sigma.team.lead_board import LeadBoard
from sigma.team.mailbox import Mailbox
from sigma.team.scanner import dep_tick, lease_tick
from sigma.team.store import BoardStore
from sigma.team.worker_board import WorkerBoard

#: runner 签名:(worker_id, description, max_rounds) → 子会话结论文本。
#: 异常上抛 = worker_lost 语义(引擎转 fail,attempts+1)。
Runner = Callable[[str, str, int], Awaitable[str]]


@dataclass
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
        summary = await self._summarize("全部任务已达终态,团队自动收摊")
        return summary

    async def stop(self, reason: str) -> str:
        """人为收摊:非终态任务按 lead 身份 cancel(级联由扫描器补齐)。"""
        self._stopping = True
        async with self._store.transaction(self._root) as board:
            for task in board.tasks:
                if task.state not in _TERMINAL:
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
        asyncio.gather(*self._tasks, return_exceptions=True)
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
            if converged:
                self._converged = True
                return
            await asyncio.sleep(self._config.poll_s)

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
                "这是共享任务板上分配给你的独立任务,完成后用 team_board 的 "
                "board op=finish 收尾没有意义——直接给出结论文本即可,"
                "引擎会替你登记;搞不定也直接报错,不要装死。"
            )
            lease = asyncio.create_task(self._heartbeat_loop(wb, claimed.id))
            try:
                result = await self._runner(
                    worker_id, description, self._config.max_rounds
                )
                await wb.finish(claimed.id, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # worker_lost 的事件驱动通道:异常即认输
                await wb.fail(claimed.id, f"{type(exc).__name__}: {exc}")
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
