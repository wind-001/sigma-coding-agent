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
from typing import Final

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

#: 连续 IO 故障多少次后往 lead 信箱告警（**告警，不是退出**）。
#:
#: 2 是算出来的，见 :meth:`TeamEngine._heartbeat_loop` 的 docstring：
#: ``lease_deadline`` = 上次成功续租 + ttl，而重试间隔是 ttl/3，
#: 所以第 2 次连续失败时最坏恰好把余量用尽。再晚就真被回收了，
#: 那时告警已经晚了——用户看到的是"任务莫名重试"而不是"IO 坏了"。
IO_FAULT_WARN_AT: Final[int] = 2


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
        # 2026-10-02:IO 故障计数与告警去重。`_on_io_fault` 永不抛,靠这两个
        # 字段把"发生过几次"与"告警有没有真的送出去"都留在内存里可查——
        # 静默计数器不够,而告警本身写盘也可能失败(见 _on_io_fault)。
        self._io_faults = 0
        self._io_alert_failures = 0
        self._notified_io_faults: set[str] = set()

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
            try:
                async with self._store.transaction(self._root) as board:
                    dep_tick(board)
                    lease_tick(board, now=self._clock())
                    tasks = board.tasks
                    converged = bool(tasks) and all(
                        task.state in _TERMINAL for task in tasks
                    )
                    fresh_failures = self._unnotified_failures(tasks)
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                # 2026-10-02 修:原实现这里没有防护,一次写盘 PermissionError
                # 就让整个 run() 冒泡终止——全量测试里那条
                # ``store.py:86 PermissionError`` 就是从这里出去的。
                # 监督协程死了等于**没人再看板上还有没有非终态任务**,
                # 团队永远不收敛,比 worker 猝死更隐蔽(连 attempts 都不涨,
                # 界面只显示"运行中")。
                await self._on_io_fault("收敛监督写盘失败", exc)
                await asyncio.sleep(self._config.poll_s)
                continue
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
        """worker 常驻循环。**IO 故障不许杀死这个协程**（2026-10-02 修）。

        这是最严重的一处猝死点：原实现里任一次写盘失败都会让协程带异常
        终止，而它**不会重试**——任务就此永远卡在 pending，
        ``_wait_converged`` 永远等不到收敛，``run()`` 挂到超时。
        错误只在 asyncio 的 ``Task exception was never retrieved`` 里,
        调用方(主 agent / 界面)什么都看不到。

        防护包在**整个 while body** 外层而不是某一处调用点,因为猝死点有
        三个:①line 237 的 tick 事务 ②``try_claim`` ③兜底里的 ``try_fail``
        ——第三个尤其阴:它自己也要写盘,IO 坏了它抛的 OSError 会从
        ``except`` 块里冒出来(而不是被同一层的 except 接住),照样猝死。
        逐点加 except 必然漏,所以在循环外层兜。
        """
        wb = WorkerBoard(
            self._store,
            worker_id,
            self._root,
            lease_ttl=self._config.lease_ttl,
            clock=self._clock,
        )
        while not self._stopping:
            try:
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
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                # 板写不进去时引擎不该退出:任务还在 pending,退出等于
                # 把它永远留在那儿。退避后重试,让瞬时占用过去就好。
                await self._on_io_fault(f"worker {worker_id} 写盘失败", exc)
                await asyncio.sleep(self._config.poll_s)

    async def _heartbeat_loop(self, wb: WorkerBoard, task_id: str) -> None:
        """lease 自动续租:worker 存活期间引擎每 TTL/3 续一次——LLM 不可见。

        **IO 故障不许杀死这个协程**（2026-10-02 修，见 ``_on_io_fault``）。
        原实现只 ``except ValueError``，于是 :meth:`BoardStore._save` 抛
        ``PermissionError``（Windows 上 ``os.replace`` 偶发 WinError 5）
        时协程带异常终止——**静默地**。后果是链式的：心跳没了 ⇒ 租约不再
        续 ⇒ ``lease_tick`` 判定超时 ⇒ 任务被误回收重派 ⇒ attempts 累积。
        而用户在界面上看到的只是"任务莫名重试"，错误本身只出现在
        asyncio 的 ``Task exception was never retrieved`` 里。

        容忍度是算过的，不是拍的：``lease_deadline`` = **上次成功续租的时刻**
        + ttl，不随失败重算，所以连续失败 N 次的最坏时刻是
        ``N × ttl/3``。N=2 时正好等于 ttl ⇒ 余量归零。所以**能容忍 1 次
        连续失败，第 2 次就有被回收的风险**——超过 :data:`IO_FAULT_WARN_AT`
        就往 lead 信箱告警（去重，不刷屏），但**仍然继续续**，不退出。
        宁可租约真的断掉（那会在 task.note 留痕），也不要心跳先死。
        """
        ttl = self._config.lease_ttl
        misses = 0
        while True:
            await asyncio.sleep(ttl / 3)
            try:
                await wb.heartbeat(task_id)
            except ValueError:
                return  # 任务已不在 running(被回收/取消)——心跳自然结束
            except OSError as exc:
                misses += 1
                if misses >= IO_FAULT_WARN_AT:
                    # 去重键**不带计数**（见 _on_io_fault）：带了就会
                    # "连续 2 次""连续 3 次"各算一条新键 ⇒ 每轮重试刷一封信箱。
                    await self._on_io_fault(f"心跳续租失败(任务 {task_id})", exc)
            else:
                misses = 0

    async def _on_io_fault(self, where: str, exc: BaseException) -> None:
        """记一次 IO 故障并（首次）往 lead 信箱告警。**永不抛**。

        为什么走信箱而不是 ``logging``/``print``：项目全局不装 logging
        （``grep import logging src/`` 只命中 node_modules），而 team 层
        唯一的用户可见通道就是 lead 信箱——``_notify_failures`` 走的也是
        它。主 agent 下一轮 inbox 即见，这与"丢弃必须可见"是同一条纪律。

        告警本身也可能撞上同一个 IO 故障（写盘就是坏在那儿），所以这里
        **吞掉一切**并在失败时留一个属性计数：宁可"告警没送出去"，
        也不能让告警动作反过来杀死调用方（心跳/worker）——那才是本末倒置。

        ``where`` 同时是去重键，所以它**不能带会变的部分**（次数、时间戳）：
        带了就会每轮重试都算"新故障"，把 lead 信箱刷爆。变的部分放
        :attr:`_io_faults` 累计计数里，需要时查引擎实例即可。
        """
        self._io_faults += 1
        if where in self._notified_io_faults:
            return
        self._notified_io_faults.add(where)
        try:
            await self._mailbox.send(
                self._root,
                to=self._lead_id,
                sender="system:engine",
                text=(
                    f"[IO 故障] {where}: {type(exc).__name__}: {exc}\n"
                    "引擎已自动重试,任务不受影响;若同一故障反复出现,"
                    "说明磁盘或权限有问题,建议检查工作区。"
                ),
            )
        except Exception:  # noqa: BLE001 - 告警失败绝不能上抛(见 docstring)
            self._io_alert_failures += 1

    async def _summarize(self, headline: str) -> str:
        board = self._store.load(self._root)
        body = board.render() if board is not None else "(板为空)"
        summary = f"{headline}\n{body}"
        await self._mailbox.send(
            self._root, to=self._lead_id, sender="system:engine", text=summary
        )
        return summary
