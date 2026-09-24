"""会话目录的目录级操作：列出、找最近的、生成新 id、生成预览。

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

**``session_previews`` 与 ``list_sessions`` 是两层，不是两个重复实现**
    前者调后者拿目录清单，**不自己 glob**——"什么算最近"只能有一处定义，
    否则 ``/sessions`` 的第一行会与 ``--continue`` 选中的会话不是同一个。
    前者额外做的事只有一件：**读每个文件的开头**，好让列表能被人认出来。
    「会话文件长什么样」这块知识因此也只在本层（``SessionPreview`` +
    ``_first_user_text``），产品壳只负责把它打印成一行。
"""

from __future__ import annotations

import json
import time
import uuid
from sigma_ai import stamps
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 会话文件的扩展名。**与 ``JsonlStore.path`` 必须一致**——
#: 这里只引用它，不重新声明一份。
SESSION_SUFFIX = ".jsonl"

#: 预览时最多读多少字节。
#:
#: **为什么是 4 KB 而不是把整个文件 load() 一遍**：``/sessions`` 要列 20 个会话，
#: 而一个长会话的文件可以是几 MB——为了显示一行摘要去反序列化全部记录，
#: 代价与收益完全不成比例。4 KB 足够覆盖开头若干条消息（含首条 user 消息）。
#:
#: **它不是一个精确上界**：截断点上可能出现半行 JSON，那半行会被丢弃
#: （见 ``_preview_one``）。**刻意不读满一整行**——"读到一条完整消息为止"
#: 需要一个"单行可以有多长"的上界，而那个上界不存在（用户可以粘贴一整个文件）。
PREVIEW_BYTES = 4096

#: 摘要截断长度（字符）。给 ``/sessions`` 一行留出空间，
#: 同时足够让人认出"这是我哪次对话"。
PREVIEW_TEXT_CHARS = 40


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


@dataclass(frozen=True)
class SessionPreview:
    """一个会话"长什么样"——给 ``/sessions`` 列表用的一行。

    **为什么是 dataclass 而不是 BaseModel**：判据与 ``SessionInfo`` / ``LoadResult``
    一致——它不落盘、不过网，只是目录扫描的返回值。

    **为什么单独一个类型，而不是给 ``SessionInfo`` 加字段**：
    ``SessionInfo`` 是"文件层面的存在"（谁在哪、多大），
    ``SessionPreview`` 是"内容层面的一瞥"（说了些什么）。
    前者是 ``list_sessions`` 的产出、被 ``--continue`` 消费，**零成本**；
    后者要**真的读盘**，是有代价的操作。把它们合成一个类型，
    就等于让所有只想找"最近一个会话"的调用方都付出读 20 个文件的代价。

    ``message_count`` 是**前 4 KB 内**数出来的条数，不是全会话的消息数——
    字段名保留短名字是因为它是给人看的列表项；**调用方若需要精确值，
    请自己 ``load()`` 并 len(records)**，不要指望这个数。
    """

    id: str
    modified: float
    size: int
    message_count: int
    first_user_text: str | None


def _first_user_text(payload: Any) -> str | None:
    """从一条已解析的记录里，取出"这是用户说的话"的纯文本。

    **为什么要挖这么多层**：磁盘上一条记录的形状是**两层**嵌套的
    （``NodeRecord`` → ``message`` → ``LlmMessageWrapper.message``）::

        {"id": ..., "parent_id": ...,
         "message": {"timestamp": ..., "role": "llm",
                     "message": {"role": "user",
                                 "content": "你好世界",
                                 "timestamp": ...}}}

    第一版只挖了一层（拿外层那个 dict 直接找 ``content``），
    于是**每一条会话的摘要都是 None**——``/sessions`` 全是
    "（还没有用户消息）"，而这个功能看起来是"还没实现"，
    不像 bug。（实测抓到的：``test_sessions_lists_disk_sessions``。）

    内层那个 ``message`` 是 agent 层的消息对象序列化结果，
    ``UserMessage`` 的形状是 ``content: str``；而 ``AssistantMessage``
    的 ``content`` 是**块列表**（``[{"type": "text", ...}]``），
    所以 ``isinstance(content, str)`` 这个判据顺带把助手消息挡在了外面——
    这正是我们要的：摘要是"用户说了什么"，不是"模型说了什么"。

    这里**刻意不 import ``UserMessage``**：会话层（L3）不认识 agent 层的
    具体消息类，只按"磁盘上那个 dict 有没有这些键"来判断——
    与 ``store.py`` "从不读取 ``parent_id`` 语义"是同一种克制。

    认不出来就返回 ``None``：**宁可少显示一行摘要，也不要为了显示它
    把整个列表搞崩**。这条与 ``list_sessions`` 的"能救多少救多少"同源。
    """
    if not isinstance(payload, dict):
        return None
    # 外层：NodeRecord.message = LlmMessageWrapper 的 dump。
    # 认 ``role == "llm"`` 这层是"每条记录的外壳"，往里走一层才是真消息。
    inner = payload.get("message")
    candidate = inner if isinstance(inner, dict) else payload
    if candidate.get("kind") != "user" and candidate.get("role") != "user":
        return None
    content = candidate.get("content")
    if not isinstance(content, str):
        return None
    text = " ".join(content.split())
    if not text:
        return None
    if len(text) > PREVIEW_TEXT_CHARS:
        text = text[:PREVIEW_TEXT_CHARS] + "…"
    return text


def _preview_one(info: SessionInfo, *, max_records: int) -> SessionPreview | None:
    """读一个会话文件的开头，拼出预览。坏文件返回 ``None``（跳过）。

    **只读前 ``PREVIEW_BYTES`` 字节**，且按"行"看待：JSONL 一行一条记录，
    所以逐行 ``json.loads`` 是安全的。截断点上的半行会解析失败——
    那正是我们要的效果（它本来就不完整）。

    ``max_records`` 是"最多数多少条"的**熔断**，与字节上界是两个独立的上界：
    字节上界管"读多少"，它管"数多久"。当前调用方传 ``PREVIEW_BYTES``
    （4 KB 内不可能有超过 4096 行），所以它是**防御性的、恒不触发**——
    留着是因为"4 KB 足够覆盖首条 user 消息"这个前提一旦被改动
    （比如改成 1 MB），没有熔断就会在一个巨型文件上白数几十万行。
    """
    try:
        with info.path.open("r", encoding="utf-8", errors="replace") as handle:
            chunk = handle.read(PREVIEW_BYTES)
    except OSError:
        # 扫描期间文件被删 / 被占用：跳过。（与 list_sessions 同源。）
        return None

    count = 0
    first_user_text: str | None = None
    for line in chunk.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            # 半行 / 坏行：跳过。这里**不发 warning**——`/sessions` 只是
            # 一瞥，真正加载时的坏行由 ``JsonlStore.load()`` 报（那里带行号）。
            # 两处都报会让同一个损坏刷两遍，用户会以为是两个问题。
            continue
        count += 1
        if first_user_text is None:
            message = payload.get("message") if isinstance(payload, dict) else None
            first_user_text = _first_user_text(message)
        if count >= max_records:
            break

    return SessionPreview(
        id=info.id,
        modified=info.modified,
        size=info.size,
        message_count=count,
        first_user_text=first_user_text,
    )


def session_previews(root: Path, *, limit: int = 20) -> list[SessionPreview]:
    """列出最近 ``limit`` 个会话，每个带一行可辨认的摘要。

    排序沿用 ``list_sessions``（修改时间倒序），**不重新实现一遍**——
    "什么算最近"只能有一处定义，否则 ``/sessions`` 的第一行会和
    ``--continue`` 选中的会话不是同一个。

    ``limit`` 是**先截断再读盘**：读 100 个文件再丢掉 80 个是纯浪费。
    """
    infos = list_sessions(root)[: max(limit, 0)]
    previews: list[SessionPreview] = []
    for info in infos:
        preview = _preview_one(info, max_records=PREVIEW_BYTES)
        if preview is not None:
            previews.append(preview)
    return previews


def new_session_id(*, clock: Callable[[], float] | None = None) -> str:
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
