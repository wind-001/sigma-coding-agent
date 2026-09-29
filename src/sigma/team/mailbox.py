"""双向信箱:追加写一行、drain-once 读走即清。

为什么 drain-once 而不是"已读标记"
    与 TaskTool 回报信箱同判据(详规 §2.2):读过的东西再出现会诱发重复处理。
    追加写 + 读走即删,没有"已读位"可腐化——删掉文件就是已读。

崩溃安全
    drain 只在**全部行都解析成功后**才删文件:半截 JSON(进程死于写入中途)
    会以 ValueError 浮出,信箱原样保留——静默清掉读不出的信箱等于吞消息。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sigma.team.board import now_stamp

#: 信箱目录(相对工作区根)。每个 agent 一个 ``<session-id>.jsonl``。
MAILBOX_DIR_RELATIVE = ".sigma/team/inbox"

#: session id → 文件名的安全字符集。Windows 文件名不容 ``\\ / : * ? " < > |``,
#: 多余的字符替换成下划线——消息正文不受影响,只有落点文件名被净化。
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class MailMessage:
    """一条信箱消息。``at`` 用注入的 clock,测试才能离线且确定。"""

    sender: str
    text: str
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {"sender": self.sender, "text": self.text, "at": self.at}

    @classmethod
    def from_dict(cls, raw: Any) -> MailMessage:
        if not isinstance(raw, dict):
            raise ValueError("信箱行不是 JSON 对象")
        for field_name in ("sender", "text", "at"):
            if field_name not in raw:
                raise ValueError(f"信箱行缺少 {field_name}")
        return cls(
            sender=str(raw["sender"]),
            text=str(raw["text"]),
            at=str(raw["at"]),
        )


def _safe_name(session_id: str) -> str:
    return _SAFE_NAME.sub("_", session_id)


class Mailbox:
    """信箱读写器。锁与 clock 随实例走(与 BoardStore 同一款处置)。"""

    def __init__(self, *, clock: Callable[[], str] | None = None) -> None:
        self._clock: Callable[[], str] = (
            clock if clock is not None else now_stamp
        )
        self._lock: asyncio.Lock | None = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------

    def inbox_path(self, workspace_root: Path, session_id: str) -> Path:
        return (
            workspace_root / MAILBOX_DIR_RELATIVE / f"{_safe_name(session_id)}.jsonl"
        )

    async def send(
        self, workspace_root: Path, *, to: str, sender: str, text: str
    ) -> MailMessage:
        """往 ``to`` 的信箱追加一行。**锁内执行**:与 drain 互斥,
        追加不会落在"读完了还没删"的窗口里。"""
        async with self._ensure_lock():
            message = MailMessage(sender=sender, text=text, at=self._clock())
            path = self.inbox_path(workspace_root, to)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
            return message

    async def drain(self, workspace_root: Path, session_id: str) -> list[MailMessage]:
        """读走本会话信箱的全部消息并删除文件(读后即清)。

        先解析后删除:任何一行解析失败,ValueError 上抛且**文件保留**——
        消息不被静默吞掉,等下一次(修复后)drain。
        """
        async with self._ensure_lock():
            path = self.inbox_path(workspace_root, session_id)
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8").splitlines()
            messages = [
                MailMessage.from_dict(json.loads(line))
                for line in lines
                if line.strip()
            ]
            path.unlink()
            return messages
