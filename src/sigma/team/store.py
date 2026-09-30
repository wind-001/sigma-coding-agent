"""任务板的落盘:原子读写(tmp + os.replace)+ 实例级 asyncio.Lock。

为什么 tmp+rename
    ``json.dumps → write_text`` 不是原子的:并发读到的可能是半截 JSON。
    先写 ``board.json.tmp`` 再 ``os.replace``(同目录 = 同卷,Windows / POSIX
    都是原子操作),读侧永远只见完整文件——G-TEAM-7。

为什么锁是实例级
    v1 全 in-process(详规 §2.2):主会话与子 agent 共享同一个 TeamBoard
    实例(registry clone 共享工具实例),因此共享这一把锁。
    **跨进程不承诺**——这是 v1 的显式边界,docstring 写明。

事务的失败语义
    :meth:`BoardStore.transaction` 只在正常退出时保存:守卫抛 ValueError 时
    板上分毫未动——"拒绝"不得留下半次写入。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from sigma.team.board import Board, now_stamp

#: 板文件的固定落点(相对工作区根)。工具自己的账本,不是用户路径,
#: 所以 ``.sigma`` 目录不存在时自动创建(与 todo 同一条处置)。
BOARD_RELATIVE: Final[str] = ".sigma/team/board.json"


class BoardStore:
    """board.json 的读写器。**锁与 clock 随实例走**——共享实例即共享互斥。"""

    def __init__(self, *, clock: Callable[[], str] | None = None) -> None:
        self._clock: Callable[[], str] = (
            clock if clock is not None else now_stamp
        )
        # 惰性创建:构造发生在事件循环外(InteractiveSession.__init__),
        # 与 TaskTool 的 Semaphore 同一款处置——测试之间也不串。
        self._lock: asyncio.Lock | None = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------

    def load(self, workspace_root: Path) -> Board | None:
        """读板。文件不存在返回 None;损坏抛 ValueError(宁可崩不要错)。"""
        path = self._path(workspace_root)
        if not path.exists():
            return None
        return Board.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @asynccontextmanager
    async def transaction(self, workspace_root: Path) -> AsyncIterator[Board]:
        """持锁的 读-改-写 事务:yield 出板,正常退出才原子保存,异常不落盘。

        claim 的原子性就落在这里:两个并发事务串行进锁,第二个读到的
        一定是第一个写完的板(详规 §2.2)。
        """
        async with self._ensure_lock():
            board = self.load(workspace_root) or Board()
            yield board
            board.updated_at = self._clock()
            self._save(workspace_root, board)

    # ------------------------------------------------------------------

    def _path(self, workspace_root: Path) -> Path:
        return workspace_root / BOARD_RELATIVE

    def _save(self, workspace_root: Path, board: Board) -> None:
        path = self._path(workspace_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(board.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
