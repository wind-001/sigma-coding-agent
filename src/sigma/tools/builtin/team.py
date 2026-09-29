"""``team_board`` 工具:团队任务板 + 双向信箱的 BaseTool 薄壳。

需求(P4-团队任务,星辰 2026-09-30 立项):主 agent 与子 agent 共享一块任务板、
互相发信箱消息。状态机与落盘在 ``sigma/team/``(详规 §2.0:board.py 迁移表 /
store.py 原子读写 / mailbox.py 信箱),本文件只有参数模型与 is_error 结果——
**逻辑与框架分离**,状态机可以直接喂单测(G-TEAM-1 逐行参数化就在那边)。

身份与"lead"别名
    调用者身份 = ``ctx.session_id``:它是 assignee、发件人、收件人。
    "lead" 是主会话的别名,由构造参数 ``lead_session_id`` 解析——
    子 agent 不必知道 lead 的真实 session-id。主会话与子 agent 经
    registry clone **共享同一个实例**,因此共享同一把 store 锁、同一块板;
    信箱按 ``ctx.session_id`` 分文件,各收各的。

v1 边界(详规 §3,docstring 写明)
    全 in-process(锁是实例级,跨进程不承诺)、无角色扮演、无自动仲裁。

为什么工具失败不抛异常
    ``sigma/team`` 按"宁可崩不要错"抛 ValueError(详规 §2.1),本壳统一
    捕获并转成 ``is_error=True`` 的 ToolResult(架构 4.2)——模型看到
    拒绝原因才能纠错,而"能纠错"是「纠错增益」这个核心指标的全部前提。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from sigma.tools.base import BaseTool
from sigma.agent.types import ToolContext, ToolResult
from sigma.team.board import apply
from sigma.team.mailbox import Mailbox
from sigma.team.store import BOARD_RELATIVE, BoardStore
from sigma.providers.messages import TextBlock

#: board 的子操作名 = 状态机事件名(详规 §2.1)——一套词汇,不做映射层。
BoardOp = Literal["create", "list", "claim", "finish", "fail", "reclaim"]


class TeamBoardParams(BaseModel):
    """三个 action 共用一个参数模型,按 ``action`` 区分必填。
    schema 进常驻区"可选栏"(G-TEAM-6),说明必须短;子操作语义在工具
    description 里,字段说明只留"谁必填"。"""

    action: Literal["board", "send", "inbox"] = Field(
        description="板;发信;收信(读后清)"
    )
    op: BoardOp = Field(default="list")
    id: str = Field(default="")
    title: str = Field(default="")
    deps: list[str] = Field(
        default_factory=list,
        description="create 可选;须全 success",
    )
    result: str = Field(default="", description="finish/fail 必填")
    note: str = Field(default="")
    to: str = Field(default="", description="send 必填:对方 id 或 lead")
    text: str = Field(default="")


def _reject(message: str, details: dict[str, Any]) -> ToolResult:
    """参数级拒绝:直接给 is_error 结果,不进状态机。"""
    return ToolResult(content=[TextBlock(text=message)], details=details, is_error=True)


def _strip_schema_noise(schema: dict[str, Any]) -> dict[str, Any]:
    """递归剥掉 schema 里的 title/default(字段 description 保留——
    那是模型唯一的使用说明)。见 :meth:`TeamBoard.json_schema`。"""
    schema.pop("title", None)
    schema.pop("default", None)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for field_schema in properties.values():
            if isinstance(field_schema, dict):
                _strip_schema_noise(field_schema)
    return schema


def _check_required(params: TeamBoardParams) -> ToolResult | None:
    """写操作的参数预检(与状态机的迁移守卫分开):空字段在这里就被拒,
    理由更直白;守卫管的是"板上的规则",这里管的是"调用是否成形"。"""
    if params.op == "list":
        return None
    if params.op == "create":
        if not params.title.strip():
            return _reject("create 需要 title(任务一句话)。", {"missing": "title"})
        return None
    if not params.id.strip():
        return _reject(f"{params.op} 需要 id(任务 id)。", {"missing": "id"})
    if params.op in ("finish", "fail") and not params.result.strip():
        return _reject(
            f"{params.op} 需要 result(结论或失败原因)。", {"missing": "result"}
        )
    if params.op == "reclaim" and not params.note.strip():
        return _reject(
            "reclaim 需要 note(接管原因,审计痕迹)。", {"missing": "note"}
        )
    return None


class TeamBoard(BaseTool):
    """团队任务板 + 信箱。写工具(改 ``.sigma/team/``),批次中严格顺序执行。"""

    name = "team_board"
    description = (
        "团队任务板+信箱。board:create 建任务;claim 认领(deps 全 success);"
        "finish/fail 收尾;reclaim 重派(仅 lead);list 查看;"
        "pending→running→success|fail。send 给 to 发信;inbox 收信(读后清)。"
    )
    read_only = False

    def json_schema(self) -> dict[str, Any]:
        """压薄 schema:剥掉 pydantic 自动加的 title/default 与类 docstring。

        常驻区每轮重付(G-TEAM-6:实测数字是预算的唯一凭据),而模型需要的
        只是字段说明与取值——title 是给 pydantic 文档看的,default 对模型
        是噪音("required" 已经说了什么是必填)。
        """
        schema = super().json_schema()
        schema.pop("description", None)
        return _strip_schema_noise(schema)

    def __init__(
        self, lead_session_id: str, *, clock: Callable[[], str] | None = None
    ) -> None:
        if not lead_session_id:
            raise ValueError(
                "TeamBoard 需要 lead_session_id(主会话 id),'lead' 别名靠它解析。"
            )
        self._lead = lead_session_id
        self._store = BoardStore(clock=clock)
        self._mailbox = Mailbox(clock=clock)

    # ------------------------------------------------------------------

    @property
    def params(self) -> type[BaseModel]:
        return TeamBoardParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(TeamBoardParams, args)
        try:
            if params.action == "board":
                return await self._board(params, ctx)
            if params.action == "send":
                return await self._send(params, ctx)
            return await self._inbox(ctx)
        except ValueError as exc:
            # sigma/team 的"宁可崩不要错"在此落成 is_error:原因给模型,异常不上抛。
            return ToolResult(
                content=[TextBlock(text=f"team_board 拒绝:{exc}")],
                details={"action": params.action, "op": params.op, "rejected": True},
                is_error=True,
            )

    # ------------------------------------------------------------------

    async def _board(self, params: TeamBoardParams, ctx: ToolContext) -> ToolResult:
        if params.op == "list":
            return self._list(ctx)
        rejection = _check_required(params)
        if rejection is not None:
            return rejection
        async with self._store.transaction(ctx.workspace_root) as board:
            task = apply(
                board,
                params.op,
                task_id=params.id,
                caller=ctx.session_id,
                lead=self._lead,
                title=params.title.strip(),
                deps=params.deps,
                result=params.result.strip(),
                note=params.note.strip(),
            )
        return ToolResult(
            content=[
                TextBlock(text=f"{task.id} 现在是 {task.state}。\n" + board.render())
            ],
            details={
                "action": "board",
                "op": params.op,
                "task": task.id,
                "state": task.state,
            },
        )

    def _list(self, ctx: ToolContext) -> ToolResult:
        board = self._store.load(ctx.workspace_root)
        if board is None or not board.tasks:
            return ToolResult(
                content=[
                    TextBlock(
                        text='任务板还是空的。用 board op="create" 建第一个任务。'
                    )
                ],
                details={"action": "board", "op": "list", "empty": True},
            )
        return ToolResult(
            content=[TextBlock(text=board.render())],
            details={"action": "board", "op": "list", "tasks": len(board.tasks)},
        )

    async def _send(self, params: TeamBoardParams, ctx: ToolContext) -> ToolResult:
        to = params.to.strip()
        text = params.text.strip()
        if not to:
            return _reject("send 需要 to(目标 session-id 或 lead)。", {"missing": "to"})
        if not text:
            return _reject("send 需要 text(消息正文)。", {"missing": "text"})
        target = self._lead if to == "lead" else to
        await self._mailbox.send(
            ctx.workspace_root, to=target, sender=ctx.session_id, text=text
        )
        return ToolResult(
            content=[TextBlock(text=f"已发送给 {to}。")],
            details={"action": "send", "to": target},
        )

    async def _inbox(self, ctx: ToolContext) -> ToolResult:
        messages = await self._mailbox.drain(ctx.workspace_root, ctx.session_id)
        if not messages:
            return ToolResult(
                content=[TextBlock(text="信箱是空的。")],
                details={"action": "inbox", "count": 0},
            )
        lines = [f"[{message.at}] {message.sender}: {message.text}" for message in messages]
        return ToolResult(
            content=[TextBlock(text="\n".join(lines))],
            details={"action": "inbox", "count": len(messages)},
        )


__all__ = ["BOARD_RELATIVE", "TeamBoard", "TeamBoardParams"]
