"""工具层的数据载体（纯数据，无行为）。

分层位置
    本模块属于 ``sigma_agent``，只依赖 ``sigma_ai``。方向 agent → ai，单向。

为什么这里**没有** ``ToolCall``
    agent 层不另定义工具调用类型，直接复用 ``sigma_ai.messages.ToolCallBlock``。
    理由见 `docs/plans/P1-批次2-4-详规.md` 3.1 节：
    字段完全一致（``id`` / ``name`` / ``arguments``），再定义一次只能靠约定保持同步，
    且会平白多出一个转换点。

为什么 ``ToolDefinition`` **不在**这个文件
    它引用 ``BaseTool``，而 ``base.py`` 需要 ``ToolResult``——
    分居两文件会形成循环导入。同模块放置是最直白的解法，
    理由见详规 3.2 节。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from sigma_ai.base import CancelToken
from sigma_ai.messages import ContentBlock, Usage

if TYPE_CHECKING:
    # 只用于注解，运行期不需要：``TurnResult`` 是 dataclass，
    # 配合 ``from __future__ import annotations``，字段注解不求值。
    # 这样 ``types`` 与 ``agent_messages`` 之间**没有运行期依赖边**，
    # 于是 ``agent_messages`` 可以安全地反向引用 ``ToolResult``（TYPE_CHECKING）。
    from sigma_agent.agent_messages import AgentMessage


class ToolResult(BaseModel):
    """工具执行结果。**两段式**（调研笔记 9.5.8 / 14.1 第 4 条）。

    ``content`` 进上下文、给模型看；``details`` 不进上下文，只给 UI / 审计 / 评测。

    **判定依据**（架构方案 4.2 节）：若一段内容不需要模型看到，
    它就必须在 ``details`` 里。这直接决定上下文预算。

    最容易写错的两处：

    1. 把退出码、耗时之类的元信息塞进 ``content``——每轮多几个 token，
       而那点信息模型未必用得上。
    2. 把 ``stderr`` 放进 ``details``——**这是功能性错误**：
       模型看不到报错就无法纠错，而"能纠错"是「纠错增益」这个核心指标的全部前提。
       见详规 3.6 节。
    """

    content: list[ContentBlock]
    details: dict[str, Any] = {}
    is_error: bool = False


class ToolContext(BaseModel):
    """工具执行上下文。

    ``signal`` 必须在 P1 就进接口——**取消与超时后加是破坏性变更**
    （调研笔记 14.1 第 5 条）。P1 的 CLI 尚未处理中断，但签名先立住，
    P3 做 steering 时不必改所有工具的签名。

    ``emit`` 在 P1 只用于 CLI 打点：P1 没有 TUI，不做流式渲染。
    用 :meth:`say` 而不是直接调 ``emit``，免得每个工具都判一次 None。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session_id: str
    workspace_root: Path
    signal: CancelToken
    emit: Callable[[str], None] | None = None

    def say(self, message: str) -> None:
        """发出一条进度信息。``emit`` 未提供时静默忽略。"""
        if self.emit is not None:
            self.emit(message)


@dataclass
class TurnResult:
    """一轮 ``run_turn`` 的结果。

    **为什么用 dataclass 而不是 BaseModel**（这是本模块唯一的例外）：

    它装的是**活的 ``AgentMessage`` 实例**，不是可落盘的元数据。
    Pydantic 的价值在校验与序列化，这两样在这里都用不上；
    而 ``AgentMessage`` 是 ``ABC``，让 Pydantic 去校验一个抽象基类字段，
    只会引入"重建实例"的风险，收益为零。

    这不是随手破例。判据与架构 0.3 节一致——**基类答"谁是谁"，
    模型答"装着什么"**，而 ``TurnResult`` 两样都不是：
    它只是把一个返回值捆绑起来，交给调用方立刻消费，不落盘、不过网。

    ``messages`` 是**本次新增**的消息（assistant + 工具结果），
    不含调用方传进来的历史——loop 参照 Pi 的形状，不持有会话对象。
    """

    status: Literal["completed", "stopped", "error"]
    messages: list[AgentMessage]
    text: str = ""
    rounds: int = 0
    usage: Usage | None = None
    reason: str = ""
