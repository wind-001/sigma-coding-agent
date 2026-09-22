"""会话目录的目录级操作：列出、找最近的、生成新 id。

分层位置
    本模块属于 ``sigma_session``（L3），只依赖标准库。

**它不决定会话放在哪。**
    ``root`` 由调用方传入——与 ``store.py`` 同一条判据：
    **谁决定策略，谁传参**（产品壳决定"放 ``~/.sigma/sessions``"，
    会话层只管"给你一个目录，我怎么在里面找"）。

    判据来源见 ``store.py`` 的模块 docstring：
    「工具层不自己去 ``Path.home()`` 猜密钥路径」是同一条的另一次应用。

**为什么需要它，而不是让 CLI 自己列目录**
    ``--continue`` 要找"最近的那个会话"，而**会话 id 与文件名的对应关系
    由本层定义**（``JsonlStore.path`` 做了净化）。让 CLI 自己拼
    ``glob("*.jsonl")`` 就等于把这个约定复制到产品壳里——
    一旦净化规则变了（比如加上目录分层），两处就会不一致，
    而症状是"``--continue`` 找不到刚存下的会话"，看起来像文件没写成功。
"""

from __future__ import annotations

import time
import uuid
from sigma_ai import stamps
from dataclasses import dataclass
from pathlib import Path

#: 会话文件的扩展名。**与 ``JsonlStore.path`` 必须一致**——
#: 这里只引用它，不重新声明一份。
SESSION_SUFFIX = ".jsonl"


@dataclass(frozen=True)
class SessionInfo:
    """一个已存在的会话文件。

    **为什么是 dataclass 而不是 BaseModel**：它不落盘、不过网，
    只是目录扫描的返回值——判据与 ``LoadResult`` / ``TurnResult`` 一致。
    """

    id: str
    path: Path
    modified: float
    size: int


def new_session_id(*, clock: object = None) -> str:
    """生成一个新的会话 id，形如 ``20260922-171420.123-a1b2``。

    **与节点 id 的取法刻意不同。** 节点 id 用纯 uuid4（``store.new_node_id``），
    因为它在多进程并发下必须不撞；而会话 id 是**给人看的**——
    ``--continue`` 之后用户会想知道"我续的是哪个会话"，
    带时间前缀的 id 既能一眼看出先后，也能直接排序。

    **时间到毫秒**（2026-09-22 星辰要求）：既能读、又比"到秒"少一次歧义。
    毫秒本身由 ``stamps.split`` 算，本函数不自己换算——
    换算散成两份就会在某一处悄悄差 1 毫秒。

    末尾四位随机**仍然保留**：毫秒并不保证唯一（脚本里连着调两次完全可能
    落在同一毫秒内），而撞了就会写到**同一个会话文件**上——
    两个会话的历史混在一起，且没有任何报错。

    ``clock`` 可注入是为了让测试确定——真实时钟会让断言无法复现。
    """
    epoch = clock() if callable(clock) else time.time()
    return f"{stamps.compact(epoch)}-{uuid.uuid4().hex[:4]}"


def session_path(root: Path, session_id: str) -> Path:
    """会话 id → 文件路径。

    **走 ``JsonlStore.path``，不自己拼**：净化的唯一实现在那里
    （见模块 docstring）。
    """
    from sigma_session.store import JsonlStore

    return JsonlStore(root, session_id).path


def list_sessions(root: Path) -> list[SessionInfo]:
    """列出 ``root`` 下的全部会话，**按修改时间倒序**（最近的在前）。

    目录不存在时返回空列表——**不是错误**：一次都没跑过就没有会话，
    与 ``store.load()`` 对"文件不存在"的处置一致。
    """
    if not root.is_dir():
        return []

    infos: list[SessionInfo] = []
    for path in root.glob(f"*{SESSION_SUFFIX}"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            # 扫描期间文件被删/被占用：跳过而不是让整个列表失败。
            # 这条与 store 的"坏行跳过"同源——**用户数据要能救多少救多少**。
            continue
        infos.append(
            SessionInfo(
                id=path.name[: -len(SESSION_SUFFIX)],
                path=path,
                modified=stat.st_mtime,
                size=stat.st_size,
            )
        )
    # 次键用 id：mtime 相同（同一秒内建两个）时排序仍**确定**，
    # 否则 `--continue` 会在两次调用之间返回不同的结果——那种不确定性
    # 会被误当成"会话丢了"。
    infos.sort(key=lambda info: (info.modified, info.id), reverse=True)
    return infos


def latest_session_id(root: Path) -> str | None:
    """最近修改的会话 id；一个都没有时返回 ``None``。

    返回 ``None`` 而不是抛错：``--continue`` 在空目录下的正确行为是
    "告诉你没有可续的会话，然后开一个新的"，不是崩掉。
    """
    sessions = list_sessions(root)
    return sessions[0].id if sessions else None
