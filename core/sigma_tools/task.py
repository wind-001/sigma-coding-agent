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

为什么回报里带 token 用量（P4-批次2，D-A1）
    子会话的 ``TurnResult.usage`` 曾经只到 ``_run_sub`` 为止——A/B 评测的成本指标
    于是只统计到主 agent（编排者），两臂真实花费不可见。用量与结论**写进同一条
    回报文本**：主 agent 是评测数据的第一个消费者，成本必须与结论一起被看到
    （与 stopped 前缀"结果可信度与结果同行"同一条纪律）。
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
from sigma_ai.messages import TextBlock, Usage, UserMessage

#: 单个子任务结果回传进主上下文的字符上限（约 1k token）。
#: 超过它说明"总结没写好"，而不是主上下文该装下它——截断且**可见**（丢弃必须可见）。
MAX_RESULT_CHARS = 4000

#: 收尾兜底等待在跑子任务的总上限（秒）。
#: 与流式总时长闸（provider 300 s）同量级、有实测凭据：单轮 3–30 s，
#: 子会话 10–30 轮 ≈ 30–300 s。它是**最后一道闸**——子任务自己还有
#: 轮数预算与流式双闸，正常路径根本碰不到它。
MAILBOX_WAIT_TIMEOUT_S = 300.0


class SubAgentRounds(BaseModel):
    """子 agent 的轮数预算：三档，由**派发的模型**按任务难度选。

    三档取值与依据（全部来自本项目实测，不是拍脑袋）
        low    = 10  覆盖"取信息、读文件给结论"这类窄子任务。依据：修 bug 类
                     短任务实测最短 7 轮（syn-002）／A/B 试水 7–9 轮；10 = 7 × 1.4。
        medium = 20  默认档，覆盖短 bug 修复（实测 7–16 轮，syn-001…010 的 B2 臂）。
                     20 = 实测最大 16 × 1.25，且与主任务默认 max_rounds 对齐。
        high   = 30  覆盖复杂子任务。依据：两条复杂任务实测 23 / 25 轮完成
                     （syn-012 / syn-011），30 = 25 × 1.2。**低于 23 会重演
                     syn-011 在 r20 的失败形状**——修到只剩 1 处 bug 被掐断。

    为什么不再保留原来的固定 50
        50 轮 × 实测每轮 3–9k prompt = 单次子任务上限约 450k token，而
        r20 → r30 的边际收益已经归零（两条复杂任务 23 / 25 轮就完成了）。
        50 是无凭据的虚高；30 有实测支撑，且省下的是真金白银。

    为什么判断难度的是模型而不是代码
        代码只能看描述长度之类的表面特征——那是循环论证（"长描述=难"没有
        证据）。**派发的模型是唯一知道任务难度的一方**：它刚拆完计划。
        低档猜错了也不致命：子会话跑不满会 stopped，回报里带"未正常收尾"
        注记，主 agent 可以立刻用 high 重派——闭环是通的。
    """

    low: int = 10
    medium: int = 20
    high: int = 30

    def for_level(self, level: str) -> int:
        """按档位名取轮数。认不出的档位**不猜**——直接报错。"""
        table: dict[str, int] = {
            "low": self.low,
            "medium": self.medium,
            "high": self.high,
        }
        try:
            return table[level]
        except KeyError:  # pragma: no cover - Literal 已约束，防御性
            raise KeyError(f"未知难度档位：{level!r}") from None

SubAgentFactory = Callable[[str, ToolContext, str, int], Awaitable[TurnResult]]
"""工厂签名：``(description, 主 ctx, 派生 session_id, max_rounds) -> 子会话 TurnResult``。

``max_rounds`` 由派发方按难度档位给出（见 :class:`SubAgentRounds`）——
轮数预算是成本闸，不能让子会话自己决定。

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
    difficulty: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="子任务难度→轮数预算：low=10 轮（取信息）、medium=20、high=30（多步修复）。",
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
    usage: Usage | None = None  # 子会话的 token 用量，随回报回传（D-A1）
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
        self,
        factory: SubAgentFactory,
        *,
        max_concurrent: int = 3,
        rounds: SubAgentRounds | None = None,
    ) -> None:
        self._factory = factory
        self._max_concurrent = max_concurrent
        self._rounds = rounds if rounds is not None else SubAgentRounds()
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
        max_rounds = self._rounds.for_level(params.difficulty)
        state.task = asyncio.create_task(
            self._run_sub(state, description, ctx, full_sub_id, max_rounds)
        )
        pending = sum(1 for st in self._tasks.values() if st.pending)
        return ToolResult(
            content=[
                TextBlock(
                    text=(
                        f"已派发子任务 #{sub_id}（后台执行中，完成后自动回报）。"
                        f"轮数预算 {max_rounds}（难度 {params.difficulty}）。"
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
                "difficulty": params.difficulty,
                "max_rounds": max_rounds,
            },
        )

    async def _run_sub(
        self, state: _SubTaskState, description: str, ctx: ToolContext,
        full_sub_id: str, max_rounds: int,
    ) -> None:
        """后台协程：限流 → 跑子会话 → 结果进信箱。异常也进信箱（不静默丢）。"""
        assert self._semaphore is not None  # _dispatch 里必然已创建
        async with self._semaphore:
            state.state = "running"
            try:
                result = await self._factory(
                    description, ctx, full_sub_id, max_rounds
                )
            except asyncio.CancelledError:
                state.state = "failed"
                state.error = "子任务被取消"
                raise
            except Exception as exc:  # 后台任务：异常必须变成信箱里的可见回报
                state.state = "failed"
                state.error = f"{type(exc).__name__}: {exc}"
                return
        state.rounds = result.rounds
        state.usage = result.usage
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

    async def wait_and_drain(
        self, timeout_s: float = MAILBOX_WAIT_TIMEOUT_S
    ) -> list[AgentMessage]:
        """收尾兜底：有在跑的子任务就等它们结束（**有上限**），然后 drain。

        等待必须有超时（2026-09-24 review 修复）：此前 ``gather`` 无上限，
        一个死锁/失联的子任务会让主 loop 永久停在收尾处——轮数预算管不到
        两轮之间，流式双闸管不到"等别人"的这段时间。

        超时不是静默丢弃：未完成的子任务标记 ``failed`` 并照常 drain——
        **主 agent 必须看得见**"有个子任务没回来"（丢弃必须可见）。
        """
        pending = [st for st in self._tasks.values() if st.pending]
        running_tasks = [st.task for st in pending if st.task is not None]
        if running_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*running_tasks, return_exceptions=True),
                    timeout=timeout_s,
                )
            except (asyncio.TimeoutError, TimeoutError):
                for st in pending:
                    # wait_for 会先取消 gather 的子任务；``_run_sub`` 的
                    # CancelledError 分支可能已把 error 写成"子任务被取消"——
                    # 覆写成真正的原因：**超时是原因，取消是手段**。
                    st.state = "failed"
                    st.error = f"等待子任务完成超时（>{timeout_s:.0f}s）"
                    if st.task is not None:
                        st.task.cancel()
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
                    + f"\n…（子任务结果超过 {MAX_RESULT_CHARS} 字符已截断，结论请让子任务写得更精炼）"
                )
            # 用量与结论同行（D-A1）：usage 为 None（回放/假工厂）时保持原文本，
            # 逐字节不变。
            usage_text = ""
            if state.usage is not None:
                usage_text = (
                    f"，token {state.usage.prompt_tokens}"
                    f"+{state.usage.completion_tokens}"
                    f"（cached {state.usage.cached_tokens}）"
                )
            body = (
                f"[子任务回报] #{state.id}「{desc_head}」"
                f"已完成（{state.rounds} 轮{usage_text}）：\n{text}"
            )
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
