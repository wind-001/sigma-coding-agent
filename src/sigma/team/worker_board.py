"""Worker 角色的板面(物理分权,详规 §2):claim / heartbeat / finish / fail。

为什么独立成模块而不是 TeamBoard 上的运行时守卫(星辰设计评审)
    "事件人人能发"是多 agent 系统最常见的死法。把 worker 能发的事件收进
    **类型上独立**的门面,越权调用从"运行时报错"变成"这个对象上根本
    没有该方法"。lead 的 create/reclaim/abandon/cancel 在
    :class:`sigma.team.lead_board.LeadBoard`——两个门面互不知晓。

持久化归门面
    每个方法都是 store 事务内的 读-改-写(与工具壳原路径同构);引擎直接
    持有门面,免掉"每个动作都过一层参数模型"的绕路——门面是**进程内 API**,
    工具壳才是模型面。

claim 是**拉**模型(v3 拍板):worker 主动从 pending 捞活,没有就返回 None
(引擎负责休眠与轮询)。deps 未就绪的 pending 是回退防御态,跳过不报错。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from sigma.team.board import CLAIM, FAIL, FINISH, HEARTBEAT, Board, TeamTask, apply
from sigma.team.store import BoardStore

#: worker 的默认租约时长(秒)。安全网,不是主路径——主动 finish/fail 才是。
DEFAULT_LEASE_TTL: float = 300.0


class WorkerBoard:
    """绑定一个 worker 身份的板面。引擎为每个常驻 worker 造一个。"""

    def __init__(
        self,
        store: BoardStore,
        worker_id: str,
        workspace_root: Path,
        *,
        lease_ttl: float = DEFAULT_LEASE_TTL,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("WorkerBoard 需要 worker_id(认领与 lease 的身份)。")
        self._store = store
        self._worker_id = worker_id
        self._root = workspace_root
        self._lease_ttl = lease_ttl
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

    async def try_claim(self) -> TeamTask | None:
        """从 pending 捞一个可认领任务;没有返回 None(引擎负责休眠)。

        deps 未就绪的 pending 是回退防御态:跳过不报错,等扫描器放行。
        """
        async with self._store.transaction(self._root) as board:
            for task in list(board.tasks):
                if task.state != "pending":
                    continue
                try:
                    return apply(
                        board,
                        CLAIM,
                        task_id=task.id,
                        caller=self._worker_id,
                        now=self._clock(),
                        lease_ttl=self._lease_ttl,
                    )
                except ValueError:
                    continue  # 该任务当前不可认领(依赖回退态),看下一个
        return None

    async def try_claim_direct(self, task_id: str) -> TeamTask:
        """指定任务认领:守卫失败抛 ValueError(测试与引擎的边界路径)。

        常规路径走 :meth:`try_claim`(跳过不可认领的 pending)。
        """
        async with self._store.transaction(self._root) as board:
            return apply(
                board,
                CLAIM,
                task_id=task_id,
                caller=self._worker_id,
                now=self._clock(),
                lease_ttl=self._lease_ttl,
            )

    async def heartbeat(self, task_id: str) -> TeamTask:
        """续租。**由引擎自动调用**(worker 存活期间每 poll 一次)——
        LLM 永远不需要知道 lease 的存在(v3.1 修订)。"""
        async with self._store.transaction(self._root) as board:
            return apply(
                board,
                HEARTBEAT,
                task_id=task_id,
                caller=self._worker_id,
                now=self._clock(),
                lease_ttl=self._lease_ttl,
            )

    async def finish(self, task_id: str, result: str) -> TeamTask:
        """任务目标达成,正常收尾(守卫:调用者==assignee)。"""
        async with self._store.transaction(self._root) as board:
            return apply(
                board,
                FINISH,
                task_id=task_id,
                caller=self._worker_id,
                result=result,
            )

    async def fail(self, task_id: str, reason: str) -> TeamTask:
        """主动认输(守卫:调用者==assignee)——**义务,不是可选项**:
        装死要等整个 lease TTL 才被回收,主动 fail 立刻放行重派。"""
        async with self._store.transaction(self._root) as board:
            return apply(
                board,
                FAIL,
                task_id=task_id,
                caller=self._worker_id,
                result=reason,
            )
