"""内存会话管理器（纯标准库，零第三方依赖）。

任务约束（本文件严格遵守）：
- 只实现 new_session / advance_step / rollback 三个动作函数；
- 不使用数据库、不做前端、不做持久化；进程退出即消失。

状态机（任务给定，仅三态）::

    ready --advance_step--> running
      ^                        |
      |------ rollback --------|

    error 为终态（当前三个公开函数均无法产出，详见 report.md 的 D2）

快照语义（任务只给了"最多 5 条"一句话，以下取值属规格空白处的裁定，
已登记在 report.md 待确认项）：
- 每次 advance_step **推进前**记录 1 条前态快照（否则最新快照等于当前
  状态，rollback 会退化成空操作）；
- 超过 MAX_SNAPSHOTS 时 FIFO 丢弃最旧一条；
- rollback 为"消费式撤销"：弹出最新快照并恢复，故回滚次数受快照条数
  限制，被淘汰的历史不可再回溯。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

MAX_SNAPSHOTS: Final[int] = 5


class SessionStatus(Enum):
    """会话状态。任务限定只能是三者之一。"""

    READY = "ready"
    RUNNING = "running"
    ERROR = "error"


class SessionError(RuntimeError):
    """会话规则违例（未知会话、非法迁移、快照不足）。"""


@dataclass(frozen=True)
class Snapshot:
    """一次已提交状态的只读快照。

    三问判定：无需 JSON schema、不落盘、不校验外部输入
    -> 载体为 frozen dataclass。
    """

    step_counter: int
    status: SessionStatus


@dataclass
class Session:
    """单会话的内存表示。snapshots 可变，故用普通 dataclass。"""

    session_id: str
    status: SessionStatus = SessionStatus.READY
    step_counter: int = 0
    snapshots: list[Snapshot] = field(default_factory=list)


class SessionManager:
    """会话集合的内存管理者。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def new_session(self, session_id: str | None = None) -> Session:
        """创建并登记一个 ready 状态的新会话。

        :param session_id: 指定 ID；None 时自动生成 uuid4 十六进制串。
        :rtype: Session
        :raises SessionError: ID 已存在。
        """
        if session_id is None:
            session_id = uuid.uuid4().hex
        if session_id in self._sessions:
            raise SessionError(f"会话 ID 已存在: {session_id}")
        session = Session(session_id=session_id)
        self._sessions[session_id] = session
        return session

    def advance_step(self, session_id: str) -> Session:
        """推进一步：先记录前态快照，再计数 +1、状态置 running。

        :rtype: Session
        :raises SessionError: 会话不存在，或处于 error 终态。
        """
        session = self._require(session_id)
        if session.status is SessionStatus.ERROR:
            raise SessionError(f"error 终态不可推进: {session_id}")

        self._record_snapshot(session)   # 先记录前态，rollback 才能撤销本步
        session.step_counter += 1
        session.status = SessionStatus.RUNNING
        return session

    def rollback(self, session_id: str) -> Session:
        """消费最新快照并恢复其状态。

        :rtype: Session
        :raises SessionError: 会话不存在、error 终态，或无可用快照。
        """
        session = self._require(session_id)
        if session.status is SessionStatus.ERROR:
            raise SessionError(f"error 终态不可回滚: {session_id}")
        if not session.snapshots:
            raise SessionError(f"没有可用快照: {session_id}")

        snapshot = session.snapshots.pop()
        session.step_counter = snapshot.step_counter
        session.status = snapshot.status
        return session

    def _require(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(f"会话不存在: {session_id}")
        return session

    @staticmethod
    def _record_snapshot(session: Session) -> None:
        """记录 session 当前（推进前）状态，超上限则 FIFO 淘汰最旧。"""
        session.snapshots.append(
            Snapshot(step_counter=session.step_counter, status=session.status)
        )
        if len(session.snapshots) > MAX_SNAPSHOTS:
            session.snapshots.pop(0)
