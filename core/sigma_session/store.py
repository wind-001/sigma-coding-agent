"""会话落盘：一个会话一个 JSONL 文件。

分层位置
    本模块属于 ``sigma_session``（L3）。只依赖 ``sigma_agent``
    （``message_to_dict`` / ``message_from_dict``）与 pydantic。

为什么一个会话一个文件，而不是全局单文件
    架构 3.1 定了"会话树 JSONL（``id`` + ``parentId``）"，但没定文件粒度。
    P2 详规 Q1 定了**一会话一文件**（``<root>/<session_id>.jsonl``），两条理由：

    1. 全局单文件在追加时是**全表锁**——多会话并发写会互相阻塞；
    2. **一处损坏拖垮所有会话**。现在是"这个会话坏了"，全局单文件是"全部都坏了"。

``NodeRecord`` 为什么定义在本模块，而不是 ``tree.py``
    它是**磁盘上那一行的形状**。存储层要能独立读写一个会话文件，
    就必须知道记录长什么样；反过来，``tree.py`` 依赖它是由树来组装记录。
    若把它放 ``tree.py``，存储层就得反向 import 树——
    那样"只想读写 JSONL"也要拖进整棵树的语义。

    **一个可检验的边界**：本模块**从不读取 ``parent_id`` 的语义**，
    只原样搬运。谁解释 `parent_id`（谁就是树）在 ``tree.py``。

**"坏行跳过 + warning" 与 ``messages_from_jsonl`` 的"坏行抛错"是两种相反取舍**

    这里**故意不**复用 ``messages_from_jsonl`` 的整体语义。两者场景不同：

    | | ``messages_from_jsonl``（在 ``sigma_agent``） | 本模块 ``load()`` |
    | --- | --- | --- |
    | 适用对象 | **测试夹具 / 内部数据** | **用户数据（磁盘上的会话）** |
    | 坏行处置 | **抛错**，且带行号 | **跳过 + warning**，记下行号 |
    | 为什么 | 夹具坏了要**立刻知道**——它是我们自己写的，坏了就是 bug | 用户数据坏了要**能救多少救多少**：一行撑不住不该让整个会话打不开 |

    这不是"两个模块风格不一致"，是**同一件事在两种数据上的两种正确处置**。
    **写在这里是为了防止后人把它"统一"掉**——统一成任一方向都会有一边出错：
    夹具侧放宽 → 坏夹具静默通过；用户侧收紧 → 一行坏数据毁掉整个会话。

    注意它与「宁可崩，不要错」也**不冲突**：那条针对的是**认不出的消息类型**
    （语义不明，猜不出该怎么处理）；这里是**已知结构里的坏行**
    （语义明确：这行废了）。批次 1.5 的 W1 决策已经把这个区分说清楚了。

为什么 ``append`` 会建父目录，而 ``WriteTool`` 不建
    两个场景的**调用方不同**：

    - ``WriteTool`` 由**模型**驱动，路径来自模型——建父目录会让它
      "打错一个目录名"变成"凭空多出一棵目录树"，所以宁可不建（详规 3.7）；
    - 本模块由**我们自己的代码**驱动，``root`` 是配置项（``~/.sigma/sessions``），
      第一次跑时它不存在是**正常的**，报错只会让每个入口都要自己 mkdir。

    判据是**信任边界**，不是"建目录好不好"。
"""

from __future__ import annotations

import json
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from sigma_agent.agent_messages import (
    AgentMessage,
    message_from_dict,
    message_to_dict,
)

DEFAULT_SESSIONS_DIR = ".sigma/sessions"
"""默认会话目录（相对仓库根）。

**刻意不在这里拼 ``Path.home()``**：落盘位置是产品壳（``sigma``）的决定，
不是会话层的决定。本模块只接受一个 ``root``。这条与批次 8 的
「工具层不自己去 ``Path.home()`` 猜密钥路径」是同一条判据——
**谁决定策略，谁传参**。
"""


class BadLineSkipped(UserWarning):
    """加载时跳过了坏行。

    **必须带上行号**，否则这条 warning 自己就成了新的"无从排查"——
    与本项目 ``UnconvertibleMessageWarning`` 的要求一致。
    """


class NodeRecord(BaseModel):
    """会话树的一个节点在磁盘上的形状。

    ``parent_id`` 为 ``None`` 表示它是**根节点**。

    ``message`` 存的是 ``message_to_dict`` 的产出（一个 dict），
    **不是** ``AgentMessage`` 实例——理由：
    ``AgentMessage`` 是 ``ABC``，让 Pydantic 去校验一个抽象基类字段
    只会引入"重建实例"的风险（与 ``TurnResult`` 用 dataclass 是同一条判据）。
    反序列化交给 ``message_from_dict``，它是**注册表驱动的唯一通道**。
    """

    id: str
    parent_id: str | None = None
    message: dict[str, Any]


@dataclass(frozen=True)
class LoadResult:
    """``load()`` 的返回值：读到的记录 + 被跳过的行号。

    **为什么是 dataclass 而不是 BaseModel**：它不落盘、不过网，
    只是把一个返回值捆绑起来交给调用方立刻消费——判据与 ``TurnResult`` 一致
    （基类答"谁是谁"，模型答"装着什么"，它两样都不是）。

    ``skipped_lines`` 必须能被调用方拿到，而不是只发一条 warning：
    warning 默认会被过滤/折叠，**计数是硬事实，warning 是软提示**。
    这条与「丢弃必须可见」同源。
    """

    records: list[NodeRecord] = field(default_factory=list)
    skipped_lines: list[int] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """没有任何行被跳过。"""
        return not self.skipped_lines


def new_node_id() -> str:
    """生成一个节点 id。

    **用 uuid4 而不是累积计数器**（P2 详规 Q1）：多进程 / 多会话同时追加时，
    计数器会撞——而撞了之后的症状是"两条消息互为父子"，极难定位。
    """
    return uuid.uuid4().hex


class JsonlStore:
    """一个会话文件的追加读写。

    只用**追加**：会话历史是事实记录，改写它会让"这条消息当时是否存在过"
    变得不可考。要改历史请开新分支（``tree.py``），不要改文件。
    """

    def __init__(self, root: Path, session_id: str) -> None:
        self._root = Path(root)
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def path(self) -> Path:
        """本会话的文件路径。

        文件名做了最小净化：``session_id`` 可能来自命令行（``--session``），
        其中若含路径分隔符就能写到 ``root`` 之外。
        **这不是安全边界**（真正的边界在 P3 钩子），只是防止
        "``--session a/b`` 报 FileNotFoundError"这类困惑性失败。
        """
        safe = self._session_id.replace("/", "_").replace("\\", "_")
        return self._root / f"{safe}.jsonl"

    def exists(self) -> bool:
        return self.path.exists()

    def append(self, records: list[NodeRecord]) -> None:
        """追加一批记录。**建父目录**——理由见模块 docstring（信任边界）。

        一次追加一行写一次、还是攒成一个 write？这里用**一次 write 多行**：
        少一次系统调用，且不会出现"写了一半留下半行"的中间态
        （半行在下次 ``load()`` 时是一条坏行，属于我们**自己制造的**损坏）。
        """
        if not records:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(
            json.dumps(record.model_dump(), ensure_ascii=False) for record in records
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")

    def load(self) -> LoadResult:
        """读回全部记录。**坏行跳过并记下行号**，不整体拒绝加载。

        三类"坏行"都在这里被跳过：

        1. 不是合法 JSON；
        2. 是合法 JSON 但缺字段 / 字段类型不对（``ValidationError``）；
        3. （语义层面的结构损坏——断链 / 环——**不在本层处理**，
           那属于 ``tree.py`` 的职责，见 ``SessionTree`` 的说明。）

        文件不存在**不是错误**：新会话就是没有文件。返回空结果。
        """
        if not self.path.exists():
            return LoadResult()

        records: list[NodeRecord] = []
        skipped: list[int] = []
        # ``errors="replace"`` 不是可有可无（2026-09-24 review 修复）：
        # 崩溃写入留下的**半个多字节字符**会让严格 UTF-8 解码在
        # ``for line in handle`` 处抛 ``UnicodeDecodeError``——那穿透到 CLI
        # 顶层就是"整个会话加载不了"，与上面"坏行跳过、能救多少救多少"
        # 的模块纪律直接矛盾。替换后的坏字节自然落进下面的
        # ``JSONDecodeError`` 跳过通道并记行号。
        # （``sessions.py`` 的 ``_preview_one`` 一直就是这么做的——这里是补齐。）
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    skipped.append(line_no)
                    continue
                try:
                    records.append(NodeRecord.model_validate(payload))
                except ValidationError:
                    skipped.append(line_no)
                    continue

        if skipped:
            warnings.warn(
                f"会话 {self._session_id!r} 的 {self.path} 有 {len(skipped)} 行"
                f"无法解析，已跳过（行号 {skipped}）。"
                "这是**用户数据**，所以选择「能救多少救多少」；"
                "若这些行来自测试夹具，请改用 messages_from_jsonl（它带行号抛错）。",
                BadLineSkipped,
                stacklevel=2,
            )

        return LoadResult(records=records, skipped_lines=skipped)


def record_of(message: AgentMessage, *, parent_id: str | None = None) -> NodeRecord:
    """把一条 agent 层消息包成待落盘的记录。

    ``message_to_dict`` 会拒绝未注册 / 子类实例（**编码要严**），
    所以这里是"这条消息能不能落盘"的唯一判定点——
    不要在这一层加宽容，宽容会产出"存得下但读不回"的数据。
    """
    return NodeRecord(
        id=new_node_id(),
        parent_id=parent_id,
        message=message_to_dict(message),
    )


def message_of(record: NodeRecord) -> AgentMessage:
    """从记录还原 agent 层消息。**唯一通道**是 ``message_from_dict``。

    不要在这里做 ``cls(**record.message)`` 之类的自建分派——
    注册表是反序列化的唯一事实来源（D3：扩展层是运行期可变状态）。
    """
    return message_from_dict(record.message)
