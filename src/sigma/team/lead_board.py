"""Lead 角色的板面(物理分权,详规 §2):create / reclaim / abandon / cancel。

Lead 不干活,只做元决策(星辰设计评审):分任务、收拾残局、剪枝。
finish/fail/claim/heartbeat 是 worker 的事——它们在
:class:`sigma.team.worker_board.WorkerBoard`,本类上**不存在**这些方法。

reclaim 的重试上限
    attempts 达到板上限(max_attempts)后 reclaim 被拒——重试上限对 lead
    一视同仁,否则形同虚设;此时用 abandon 终结(或先调高板上限)。
"""

from __future__ import annotations

from pathlib import Path

from sigma.team.board import (
    ABANDON,
    CANCEL,
    CREATE,
    RECLAIM,
    Board,
    TeamTask,
    apply,
)
from sigma.team.store import BoardStore


class LeadBoard:
    """绑定 lead 身份的板面。主会话(引擎)持有。"""

    def __init__(
        self,
        store: BoardStore,
        lead_id: str,
        workspace_root: Path,
    ) -> None:
        if not lead_id:
            raise ValueError("LeadBoard 需要 lead_id(主会话 id)。")
        self._store = store
        self._lead_id = lead_id
        self._root = workspace_root

    async def create(self, title: str, deps: list[str] | None = None) -> TeamTask:
        """建任务。deps 全 success → pending;未就绪 → blocked(v3 迁移表)。"""
        async with self._store.transaction(self._root) as board:
            return apply(
                board,
                CREATE,
                caller=self._lead_id,
                title=title,
                deps=deps or (),
            )

    async def reclaim(self, task_id: str, note: str = "") -> TeamTask:
        """强制接管:清 assignee 回 pending(重试上限内)。"""
        async with self._store.transaction(self._root) as board:
            return apply(
                board,
                RECLAIM,
                task_id=task_id,
                caller=self._lead_id,
                lead=self._lead_id,
                note=note or "lead 重派",
            )

    async def abandon(self, task_id: str, note: str = "") -> TeamTask:
        """终结失败任务进 dead(级联取消下游由扫描器补齐)。"""
        async with self._store.transaction(self._root) as board:
            return apply(
                board,
                ABANDON,
                task_id=task_id,
                caller=self._lead_id,
                lead=self._lead_id,
                note=note or "lead 放弃",
            )

    async def cancel(self, task_id: str, note: str = "") -> TeamTask:
        """人为撤销(需求变更/下游没意义)。"""
        async with self._store.transaction(self._root) as board:
            return apply(
                board,
                CANCEL,
                task_id=task_id,
                caller=self._lead_id,
                lead=self._lead_id,
                note=note or "lead 撤销",
            )

    async def render(self) -> str:
        """看板全文(lead 的 status 视图与工具 list 共用同一渲染)。"""
        board: Board | None = self._store.load(self._root)
        if board is None:
            return "任务板还是空的。"
        return board.render()
