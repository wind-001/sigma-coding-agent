"""``multi_agent`` 工具:团队协作模式的**引擎触发入口**(v3,星辰拍板)。

语义(详规 §3)
    主 agent 判定"当前任务复杂 / 值得并行"时调用——触发即声明团队模式。
    start 启动引擎(建板 + N 个常驻 worker 认领),默认随 ``--sub-agent``
    注册,``--no-team`` 关。完成后汇总自动写 lead 信箱,主 agent 下一轮
    inbox 即见——不需要手动收摊(stop 也随时可用)。

与 task 工具的分界
    task = push 单任务派发(一子任务一上下文);multi_agent = pull 团队
    (worker 池从共享板认领)。两档并存,由主 agent 按复杂度选。

资源归属(为什么 store/mailbox 由构造注入)
    引擎与 team_board(lead 面)**共享同一块板、同一把锁**——两个实例
    各造各的 BoardStore,锁就分成两把,互斥成为名义上的(与 G-TEAM-2
    同一条纪律)。sdk 组装时造一份传两家。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from sigma.agent.types import ToolContext, ToolResult
from sigma.providers.messages import TextBlock
from sigma.team.engine import EngineConfig, TeamEngine
from sigma.team.mailbox import Mailbox
from sigma.team.store import BoardStore
from sigma.tools.base import BaseTool


class MultiAgentParams(BaseModel):
    """三个 action 共用一个参数模型。说明刻意短——schema 进常驻区可选栏。"""

    action: Literal["start", "status", "stop"] = Field(
        description="start 启动团队;status 看板+引擎;stop 收摊"
    )
    goal: str = Field(default="", description="start 必填:团队总目标一句话")
    workers: int = Field(default=2, description="worker 数,2-3")
    reason: str = Field(default="", description="stop 可选:收摊原因")


def _reject(message: str, details: dict[str, Any]) -> ToolResult:
    return ToolResult(content=[TextBlock(text=message)], details=details, is_error=True)


def _strip_schema_noise(schema: dict[str, Any]) -> dict[str, Any]:
    schema.pop("title", None)
    schema.pop("default", None)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for field_schema in properties.values():
            if isinstance(field_schema, dict):
                _strip_schema_noise(field_schema)
    return schema


class MultiAgentTool(BaseTool):
    """团队协作引擎的入口。写工具(建板/改板),批次中严格顺序执行。"""

    name = "multi_agent"
    description = (
        "团队协作引擎入口。start 按总目标建板并启动 worker 池(认领制,"
        "并行执行);status 看进度;stop 收摊(未完任务取消)。适合可拆分、"
        "值得并行的复杂任务;简单任务用 task 派单个子 agent 即可。"
    )
    read_only = False

    def json_schema(self) -> dict[str, Any]:
        schema = super().json_schema()
        schema.pop("description", None)
        return _strip_schema_noise(schema)

    def __init__(
        self,
        *,
        lead_session_id: str,
        workspace_root: Any,
        factory: Any,
        store: BoardStore,
        mailbox: Mailbox,
        max_rounds: int = 16,
        poll_s: float = 2.0,
        lease_ttl: float = 300.0,
    ) -> None:
        if not lead_session_id:
            raise ValueError("MultiAgentTool 需要 lead_session_id(主会话 id)。")
        self._lead = lead_session_id
        self._root = workspace_root
        self._factory = factory
        self._store = store
        self._mailbox = mailbox
        self._max_rounds = max_rounds
        self._poll_s = poll_s
        self._lease_ttl = lease_ttl
        self._engine: TeamEngine | None = None
        self._engine_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------

    @property
    def params(self) -> type[BaseModel]:
        return MultiAgentParams

    @property
    def engine_running(self) -> bool:
        return self._engine is not None and self._engine.running

    async def aclose(self) -> None:
        """会话收尾:引擎若还在跑,按 lead 身份收摊(不留孤儿协程)。"""
        if self._engine is not None and self._engine.running:
            await self._engine.stop("会话关闭")

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(MultiAgentParams, args)
        try:
            if params.action == "start":
                return await self._start(params, ctx)
            if params.action == "stop":
                return await self._stop(params)
            return await self._status()
        except ValueError as exc:
            return ToolResult(
                content=[TextBlock(text=f"multi_agent 拒绝:{exc}")],
                details={"action": params.action, "rejected": True},
                is_error=True,
            )

    # ------------------------------------------------------------------

    async def _start(self, params: MultiAgentParams, ctx: ToolContext) -> ToolResult:
        if self.engine_running:
            return _reject(
                "团队已在运行(先 stop 再重新 start)。", {"already_running": True}
            )
        goal = params.goal.strip()
        if not goal:
            return _reject("start 需要 goal(团队总目标一句话)。", {"missing": "goal"})
        worker_count = max(1, min(params.workers, 3))
        # runner:引擎的 worker 执行通道——复用与 task 相同的隔离子会话工厂
        # (克隆 registry、同款安全边界);signal 沿用本次调用的 ctx。
        async def runner(
            worker_id: str, description: str, max_rounds: int
        ) -> str:
            turn = await self._factory(description, ctx, worker_id, max_rounds)
            text: str = turn.text
            return text

        engine = TeamEngine(
            goal=goal,
            lead_id=self._lead,
            workspace_root=self._root,
            store=self._store,
            mailbox=self._mailbox,
            runner=runner,
            config=EngineConfig(
                workers=worker_count,
                poll_s=self._poll_s,
                lease_ttl=self._lease_ttl,
                max_rounds=self._max_rounds,
            ),
        )
        self._engine = engine

        async def _drive() -> None:
            await engine.run()

        self._engine_task = asyncio.create_task(_drive())
        return ToolResult(
            content=[
                TextBlock(
                    text=(
                        f"团队已启动:{worker_count} 个 worker 认领制并行。"
                        "用 team_board 的 board op=create 往板上加任务"
                        "(deps 未就绪会自动挂起);multi_agent action=status "
                        "看进度;全部任务到终态后汇总自动进你的信箱。"
                    )
                ),
            ],
            details={
                "action": "start",
                "goal": goal,
                "workers": worker_count,
            },
        )

    async def _status(self) -> ToolResult:
        if self._engine is None:
            return ToolResult(
                content=[TextBlock(text="团队未启动(用 action=start)。")],
                details={"action": "status", "running": False},
            )
        return ToolResult(
            content=[TextBlock(text=self._engine.snapshot())],
            details={"action": "status", "running": self.engine_running},
        )

    async def _stop(self, params: MultiAgentParams) -> ToolResult:
        if self._engine is None:
            return ToolResult(
                content=[TextBlock(text="团队未启动,无需 stop。")],
                details={"action": "stop", "running": False},
            )
        engine, self._engine = self._engine, None
        if self._engine_task is not None and not self._engine_task.done():
            # stop() 内部会收敛 worker;这里等主协程退出并吃掉异常
            summary = await engine.stop(params.reason.strip())
            try:
                await self._engine_task
            except asyncio.CancelledError:
                pass
            return ToolResult(
                content=[TextBlock(text=summary)],
                details={"action": "stop"},
            )
        summary = await engine.stop(params.reason.strip())
        return ToolResult(
            content=[TextBlock(text=summary)],
            details={"action": "stop"},
        )


__all__ = ["MultiAgentTool", "MultiAgentParams"]
