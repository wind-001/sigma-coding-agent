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

写盘为什么会重试（2026-10-02 加）
    实测 ``os.replace`` 在 Windows 上会**偶发** ``PermissionError: [WinError 5]
    拒绝访问``——杀软/索引服务/EPERM 竞争都可能把刚写好的 tmp 或目标文件
    短时占住。40 轮引擎实跑（poll_s=0.02，约 1000 次写盘）里命中 1 次。

    为什么这不能只是"环境问题"记下:一次失败会顺着
    ``transaction`` 的 ``__aexit__`` 冒泡,而 :meth:`_save` 是**持锁**期间
    调用的 ⇒ worker/心跳协程直接猝死 ⇒ 任务卡在 pending 永不推进,
    错误只出现在 asyncio 的 "Task exception was never retrieved" 里,
    **调用方什么都看不到**。重试是治本那一半:瞬时占用重试即过。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final


#: ``os.replace`` 瞬时失败时的重试预算（次）。
#:
#: 为什么是 5 次而不是"重试到成功":真失败场景（磁盘满、目录只读）下
#: 无界重试会把每个事务都拖成卡死，而那本来就该立刻报错。
#: 实测命中的占用是**瞬时**的（杀软/索引器扫一下就走），
#: 5 次 × 递增间隔合计约 155 ms，足以覆盖。
REPLACE_ATTEMPTS: Final[int] = 5

#: 重试间隔（秒），按次递增：5 / 10 / 20 / 40 ms。
#:
#: 固定间隔不行——固定 5 ms 时，五次全落在同一个杀软扫描窗口里；
#: 递增让第 k 次必然落在更靠后的时刻。
REPLACE_BACKOFF_S: Final[tuple[float, ...]] = (0.005, 0.010, 0.020, 0.040)

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
        self._replace_with_retry(tmp, path)

    @staticmethod
    def _replace_with_retry(tmp: Path, path: Path) -> None:
        """``os.replace`` + 退避重试（见模块 docstring 的"写盘为什么会重试"）。

        捕获整个 :class:`OSError` 家族，不按 errno 筛选。理由是**漏判的代价
        远大于误判**：误判只是多花 155 ms（真失败场景下本来也要报错），
        漏判则是协程猝死、任务卡死、无人知晓。
        ``FileNotFoundError``/``NotADirectoryError`` 其实是路径写错、重试
        无意义——但那类错在开发期第一次就会暴露，不会混在生产里。

        重试耗尽后**原样重抛**最后一个异常：调用方看到的类型与 errno 与
        今天完全一致，不在这一层包自己的异常类型（那会让上层所有
        ``except`` 失配）。所以重试对可观测性是中性的——只有真持续失败
        才冒泡，而那时它本来就该冒泡。
        """
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                idx = min(attempt, len(REPLACE_BACKOFF_S) - 1)
                time.sleep(REPLACE_BACKOFF_S[idx])
        # 到不了这里：最后一次 attempt 必然 raise。真走到这行说明
        # REPLACE_ATTEMPTS <= 0（常量配错），让断言喊出来而不是静默返回。
        raise AssertionError(
            f"REPLACE_ATTEMPTS={REPLACE_ATTEMPTS} 配错，循环不可能空转"
        )
