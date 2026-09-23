"""``task`` 工具：把子任务派给后台 sub_agent（独立上下文），结果走信箱自动回报。

需求（星辰，2026-09-23，三轮补充拍板）：
    1. 主 agent 派 sub_agent 干活，**何时调用由模型根据任务驱动**；
    2. 子 agent 享有与主 agent 相同的能力（工具集相同），但**上下文隔离**——
       中间试错信息全部留在子会话里，只有结论回传；
    3. **工具中不允许包含 task**（子 agent 不得再派发）——构造上排除，不是运行期检查；
    4. 允许同时多个 sub_agent（**最多 3 个**，token 成本闸），彼此并发、互相隔离；
    5. 多个子 agent 可能有读写冲突，**用互斥锁**（批次级，loop 持有，主子共用）；
    6. **后台执行、不阻塞主 agent**：dispatch 立即返回，主 agent 继续干别的；
    7. 结果进**信箱**，由主 agent **下一轮 turn 开始之前**的钩子收集——
       只有子任务是主 agent 剩余任务的前置依赖时才需要等它（判断归模型）。

为什么 dispatch/status 是 read_only
    两个动作都不碰文件系统——dispatch 只是启动后台任务。这保证它们在 loop 的
    readonly 并发组里执行、**不持 tool_lock**：派发不能被在跑的写批次卡住，
    收尾兜底时 loop 也不持锁（否则等子任务会死锁）。子 agent 的写发生在
    子 loop 自己的批次里，那里拿锁。

为什么结果必须"信箱 + 钩子收集"而不是让模型轮询
    模型 dispatch 之后可能直接输出"已派发"收尾——靠模型主动收割，
    结果会永远留在信箱里且没有任何一步报错。loop 的收尾兜底
    （``AgentLoop._drain_or_wait_mailbox``）保证：**run_turn 的结束条件 =
    模型不再调工具 且 信箱没有未回报内容 且 没有在跑的子任务**。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, cast

from pydantic import BaseModel, Field

from sigma_agent.agent_messages import AgentMessage, LlmMessageWrapper
from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult, TurnResult
from sigma_ai import stamps
from sigma_ai.messages import TextBlock, UserMessage

#: 单个子任务结果回传进主上下文的字符上限（约 1k token）。
#: 超过它说明"总结没写好"，而不是主上下文该装下它——截断且**可见**（丢弃必须可见）。
MAX_RESULT_CHARS = 4000

SubAgentFactory = Callable[[str, ToolContext, str], Awaitable[TurnResult]]
"""工厂签名：``(description, 主 ctx, 派生 session_id) -> 子会话 TurnResult``。

由产品壳（sigma.sdk.InteractiveSession）注入：组装子会话需要 provider、keys、
skills 目录、shadow_git_dir——全是产品壳的知识，sigma_tools 反依赖 sigma 层会成环。
"""


class TaskParams(BaseModel):
    """两个动作共用一个参数模型。schema 进常驻区（D4），说明必须短——
    三个说明字段合计是预算大头（初版 93 token，压后 ~60）。"""

    action: Literal["dispatch", "status"] = Field(
        description="dispatch=派发；status=查进度。"
    )
    description: str = Field(
        default="",
        description="dispatch 必填：自包含的子任务描述（子 agent 看不到主对话）。",
    )


@dataclass
class _SubTaskState:
    """一个后台子任务的状态。信箱 = 其中 completed/failed 且未 delivered 的。"""

    id: str
    description: str
    state: str = "queued"  # queued | running | completed | failed
    rounds: int = 0
    result_text: str = ""
    error: str = ""
    delivered: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def pending(self) -> bool:
        return self.state in ("queued", "running")


class TaskTool(BaseTool):
    """子任务派发工具（壳）。read_only=True：dispatch/status 都不碰文件系统。"""

    name = "task"
    description = (
        "派子任务给后台子 agent（独立上下文、同款工具）执行，不阻塞你；完成后自动回报。"
    )
    read_only = True

    def __init__(
        self, factory: SubAgentFactory, *, max_concurrent: int = 3
    ) -> None:
        self._factory = factory
        self._max_concurrent = max_concurrent
        # Semaphore 惰性创建：构造发生在事件循环外（InteractiveSession.__init__），
        # 惰性绑定当前 loop，测试之间也不串。
        self._semaphore: asyncio.Semaphore | None = None
        self._counter = 0
        self._tasks: dict[str, _SubTaskState] = {}

    # ------------------------------------------------------------------

    @property
    def params(self) -> type[BaseModel]:
        return TaskParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(TaskParams, args)
        if params.action == "dispatch":
            return self._dispatch(params, ctx)
        return self._status()

    # ------------------------------------------------------------------
    # dispatch：启动后台子 agent，立即返回
    # ------------------------------------------------------------------

    def _dispatch(self, params: TaskParams, ctx: ToolContext) -> ToolResult:
        description = params.description.strip()
        if not description:
            return ToolResult(
                content=[TextBlock(text="dispatch 需要 description（子任务完整描述）。")],
                details={"action": "dispatch", "missing": "description"},
                is_error=True,
            )
        self._counter += 1
        sub_id = f"sa{self._counter}"
        # 派生 session_id 带主会话 id：日志与评测里能把子会话对回"谁派的第几个"。
        full_sub_id = f"{ctx.session_id}-{sub_id}"
        state = _SubTaskState(id=sub_id, description=description)
        self._tasks[sub_id] = state
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
        state.task = asyncio.create_task(
            self._run_sub(state, description, ctx, full_sub_id)
        )
        pending = sum(1 for st in self._tasks.values() if st.pending)
        return ToolResult(
            content=[
                TextBlock(
                    text=(
                        f"已派发子任务 #{sub_id}（后台执行中，完成后自动回报）。"
                        f"当前 {pending} 个未完成（并发上限 {self._max_concurrent}，"
                        "超出的会排队）。你可以继续其他步骤，"
                        f"用 task(status) 查进度。"
                    )
                )
            ],
            details={
                "action": "dispatch",
                "sub_id": sub_id,
                "sub_session_id": full_sub_id,
                "pending": pending,
            },
        )

    async def _run_sub(
        self, state: _SubTaskState, description: str, ctx: ToolContext,
        full_sub_id: str,
    ) -> None:
        """后台协程：限流 → 跑子会话 → 结果进信箱。异常也进信箱（不静默丢）。"""
        assert self._semaphore is not None  # _dispatch 里必然已创建
        async with self._semaphore:
            state.state = "running"
            try:
                result = await self._factory(description, ctx, full_sub_id)
            except asyncio.CancelledError:
                state.state = "failed"
                state.error = "子任务被取消"
                raise
            except Exception as exc:  # 后台任务：异常必须变成信箱里的可见回报
                state.state = "failed"
                state.error = f"{type(exc).__name__}: {exc}"
                return
        state.rounds = result.rounds
        if result.status == "stopped":
            # 结果可信度必须与结果本身一起交给模型："跑不起来"≠"任务失败"的镜像。
            state.result_text = (
                "[子任务未正常收尾：达到轮数上限，以下内容可能不完整]\n"
                + (result.text or "(子任务没有返回文本)")
            )
        else:
            state.result_text = result.text or "(子任务没有返回文本)"
        state.state = "completed"

    # ------------------------------------------------------------------
    # 信箱：loop 的两个钩子从这里接（产品壳把方法引用传给 AgentLoop）
    # ------------------------------------------------------------------

    def drain_completed(self) -> list[AgentMessage]:
        """非阻塞取走"已完成且未回报"的子任务，每个渲染成一条尾部 user 消息。

        drain 后标记 delivered——同一结果只回报一次（状态字在实例里，
        与实例同生命周期，跨 run_turn 保持）。
        """
        messages: list[AgentMessage] = []
        for state in self._tasks.values():
            if state.state in ("completed", "failed") and not state.delivered:
                state.delivered = True
                messages.append(self._report_message(state))
        return messages

    async def wait_and_drain(self) -> list[AgentMessage]:
        """收尾兜底：有在跑的子任务就等它们全部结束，然后 drain。"""
        pending = [st for st in self._tasks.values() if st.pending]
        running_tasks = [st.task for st in pending if st.task is not None]
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)
        return self.drain_completed()

    def _report_message(self, state: _SubTaskState) -> AgentMessage:
        desc_head = state.description[:50] + ("…" if len(state.description) > 50 else "")
        if state.state == "failed":
            body = f"[子任务回报] #{state.id}「{desc_head}」执行失败：{state.error}"
        else:
            text = state.result_text
            if len(text) > MAX_RESULT_CHARS:
                text = (
                    text[:MAX_RESULT_CHARS]
                    + "\n…（子任务结果超过 4000 字符已截断，结论请让子任务写得更精炼）"
                )
            body = f"[子任务回报] #{state.id}「{desc_head}」已完成（{state.rounds} 轮）：\n{text}"
        now = stamps.now()
        return LlmMessageWrapper(
            timestamp=now, message=UserMessage(content=body, timestamp=now)
        )

    # ------------------------------------------------------------------

    def _status(self) -> ToolResult:
        if not self._tasks:
            return ToolResult(
                content=[
                    TextBlock(
                        text="还没有派发过子任务。用 task(action=\"dispatch\", "
                        "description=...) 派发。"
                    )
                ],
                details={"action": "status", "empty": True},
            )
        counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
        for st in self._tasks.values():
            counts[st.state] += 1
        lines = [
            f"子任务（{counts['running']} running, {counts['queued']} queued, "
            f"{counts['completed']} completed, {counts['failed']} failed）："
        ]
        for st in self._tasks.values():
            mark = "已回报" if st.delivered else ("待回报" if st.state == "completed" else "")
            suffix = f"（{mark}）" if mark else ""
            lines.append(f"  #{st.id} [{st.state}]{suffix} {st.description[:40]}")
        return ToolResult(
            content=[TextBlock(text="\n".join(lines))],
            details={"action": "status", **counts},
        )

    # 测试与观测用：还有多少个子任务没走完（不含已结束未回报的）。
    def pending_count(self) -> int:
        return sum(1 for st in self._tasks.values() if st.pending)
