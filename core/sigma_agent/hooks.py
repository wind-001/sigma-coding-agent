"""钩子系统：**唯一的行为与观测扩展面**——钩子点 = 触发时机，事件驱动。

**为什么是"唯一"（P4-批次6，星辰拍板，推翻批次5 的"两通道"判据）**
    钩子不仅控制 loop 走向（将来的审批/拦截），**日志记录并告知也是钩子的
    义务**。"渲染"不过是一个订阅了日志事件的钩子，与持久化钩子平级。
    原来的 observer 通道（observe.py）已于本批并入：``LoopObserver`` 与
    ``_notify`` 删除，事件 dataclass 迁到这里，W9 判据（单方法 + 事件对象）
    由 ``BaseHook.on_event`` 继承。

    接受的代价（写明不藏）：渲染路径依赖钩子注册——不注册渲染钩子就没有
    输出。这是特性：评测与子 agent 测试天然静默，CLI 注册了才有人看。

**三条设计判据**

1. **钩子点是一个触发时机，由事件驱动**（星辰 2026-09-26）：
   事件类型本身 = 时机。新增钩子点 = 新增一个事件 dataclass +
   loop 里一处 ``emit``；"所有 hook 都要加一个方法"的那种接口不会出现。
2. **事件是瞬时通知，不是领域模型**：用 frozen dataclass（判据与
   observe.py / TurnResult 一致——不落盘、不校验；真正落盘的
   ``AgentMessage`` 在载荷里，那才是 BaseModel）。
3. **异常向上传播**（批次5 Q2 拍板）：持久化失败还继续跑 = 审计链分叉。
   **渲染类钩子自己负责宽容**（G34/G99：TerminalRenderer 内部降级），
   总线不代吞——宽容是订阅者的策略，不是总线的。

**分层位置**
    住在 ``sigma_agent``：事件载荷是 agent 层消息，发出方（loop）在本层。
    ``sigma_session`` / 产品壳向下 import 本模块是允许的依赖方向；
    本模块**不认识**会话、持久化与 rich。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable

from pydantic import BaseModel

from sigma_agent.agent_messages import AgentMessage


@dataclass(frozen=True)
class AssistantProduced:
    """钩子点：**每一次 LLM 返回**。

    触发位置在错误判断**之前**——错误轮的 partial 内容也先经此事件
    落盘再被定性（"留下审计"与"如实报错"两件事都做，与现状口径一致）。

    ``message`` 是进入 produced 的那条 agent 层消息（``LlmMessageWrapper``
    包装的 ``AssistantMessage``）。事件带包装后的形态是因为**订阅方要的
    就是"将被追加进历史的那条消息"**——拆开再包回去会出现两份包装逻辑。
    """

    message: AgentMessage


@dataclass(frozen=True)
class MessageInjected:
    """钩子点：steering 提醒 / 信箱回报等**注入**进 produced 的消息。

    为什么单独一个点：信箱是 drain-once——子任务结果从 TaskTool 取走的
    那一刻起只存在于内存，不在此刻落盘，中断就真丢了。
    """

    message: AgentMessage


@dataclass(frozen=True)
class TextChunk:
    """钩子点：模型产出的文本**增量**。**逐块透传，不聚合**——聚合了就没有"流式"了。"""

    text: str


@dataclass(frozen=True)
class ThinkingChunk:
    """钩子点：思考增量。P1 的 OpenAI 兼容 provider 不产生它，类型先留着不给后来人挖坑。"""

    text: str


@dataclass(frozen=True)
class LlmRequested:
    """钩子点：**即将发起一次模型请求**（P5-批次1，观测）。

    为什么在 loop 发、而不是 provider 内部测：``sigma_ai`` 不认识钩子
    （import-linter 契约），且"换 provider 不改测量点"是回放口径的前提。
    与 :class:`AssistantProduced`（返回）及首个 :class:`TextChunk`（首 token）
    配对，派生单次调用延迟与 TTFT——**配对与计时都归订阅者**（TraceHook），
    事件本身不带时间：事件是瞬时通知，不是领域模型（判据 2）。
    """


@dataclass(frozen=True)
class ToolStart:
    """钩子点：一次工具调用**即将执行**。

    在执行**之前**发而不是之后：模型"决定调什么"本身就是过程的一部分，
    而 bash 最长 60 s，执行期间终端一片空白会让人以为卡死。
    保证：同一批次里所有 ``ToolStart`` 都早于任何 :class:`ToolEnd`
    （顺序错了，终端会"先出结果后出调用"）。
    """

    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class ToolEnd:
    """钩子点：**每一个工具结果落定**（含 unparsed 合成的失败结果）。

    由批次5 的 ``ToolResultProduced`` 与观测通道的 ``ToolEnd`` 合并而来
    （P4-批次6）：**持久化与渲染是同一个时机的两个订阅者**，不该发两次
    事件、更不该有两个名字。``message`` 给持久化（要落盘的那条消息），
    ``name/ok/preview`` 给渲染（一行可读摘要）。
    """

    name: str
    ok: bool
    preview: str
    message: AgentMessage


@dataclass(frozen=True)
class TurnEnd:
    """钩子点：一次 ``run_turn`` 结束。带 usage 是为了让终端提示上下文压力（R1）。"""

    status: str
    rounds: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class ApprovalDecided:
    """钩子点：一次审批询问的**结论**（P5-批次1，观测）。

    批准与拒绝都发——"allowlist 命中放行"与"命中危险模式被拒"同为
    需要追溯的观测事实；此前审批决策零留痕，会话 JSONL 里只能看到
    拒绝结果的间接影子（``details={"approval": "denied"}``），放行则完全不可见。

    ``decision`` 的类型 ``ApprovalDecision`` 定义在本文件更下方（决策型
    钩子区）——注解是前向引用字符串，dataclass 不在运行时解析它；
    而 ``HookEvent`` union 在运行时做 ``isinstance`` 判别，只需本类自身
    是真实类对象即可。
    **不带 arguments**：reason 已携带"命中了什么"，参数原样落盘是
    隐私与体积的双重负担（工具参数里可能有整段代码）。
    """

    name: str
    decision: "ApprovalDecision"


HookEvent = (
    AssistantProduced
    | MessageInjected
    | TextChunk
    | ThinkingChunk
    | LlmRequested
    | ToolStart
    | ToolEnd
    | TurnEnd
    | ApprovalDecided
)
"""全部钩子点。**新增时机 = 新增一个成员 + loop 里一处 emit**，接口不变。"""


class BaseHook(ABC):
    """所有钩子的抽象基类（架构 3.1 预留位的正式落地）。

    **两个抽象方法都没有默认实现，这是刻意的**，但理由与 Provider/Tool
    （架构 4.1 "忘实现=静默错误"）不同：钩子是**订阅者**，
    ``events()`` 空着 = 订阅一切却什么都不做的僵尸、
    ``on_event`` 空着 = 假装订阅实际不处理——两者都是"看起来接入了
    实际没接入"的静默失败，必须实例化时就逼着写出来。

    ``on_event`` 同步异步都支持：同步实现返回 ``None``；
    ``async def`` 实现返回 coroutine，由 :meth:`HookManager.emit` await。
    异步位留给 P3-批次2 的审批类钩子；本期内置钩子全是同步。
    """

    #: 注册表里的名字。重名拒绝（与 ToolRegistry 同判据：
    #: 同名钩子静默重复会让"这条记录是谁写的"无法追责）。
    name: str = ""

    @abstractmethod
    def events(self) -> tuple[type[HookEvent], ...]:
        """声明订阅的钩子点（事件类型）。未列出的时机不会派发给本钩子。"""
        raise NotImplementedError

    @abstractmethod
    def on_event(self, event: HookEvent) -> Awaitable[None] | None:
        """处理一个已订阅的事件。同步返回 None；异步用 ``async def``。"""
        raise NotImplementedError


class ApprovalDecision(BaseModel):
    """一次审批的**决定**。

    ``allowed=False`` 时 ``reason`` 必填——它会进工具结果,模型要能读到
    "为什么被拒"才能自我纠正(与"单个失败不中断批次"同构)。

    ``approve_outside``:本次调用需要**越出工作区**(write/edit 的目标路径
    在工作区之外)且审批方明示豁免 → loop 把它传进 ``ToolContext``,
    L1(``resolve_write_path``)据此放行。默认 False:L1 的拒绝语义不动。
    """

    allowed: bool
    reason: str = ""
    note: str = ""
    approve_outside: bool = False


class ApprovalHook(ABC):
    """**决策型**钩子:在工具执行前给出放行/拒绝的判断(P3-批次2,L3)。

    与 :class:`BaseHook` 的区别:通知型钩子是"发生了一件事,你看着办";
    审批钩子是"这件事能不能发生,你要给一个决定"。两者注册进**同一条
    总线**(HookManager),但走各自的名单与询问协议——统一管理,职责分开。

    实现方负责自己的宽容与阻塞策略:交互式实现(终端确认)会阻塞等待
    用户输入,自动实现(allowlist 命中)立即返回。总线**不吞异常**:
    审批通道本身坏了(交互设施崩了)应该暴露,而不是静默放行。
    """

    name: str

    @abstractmethod
    async def approve(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ApprovalDecision:
        """对一个即将执行的工具调用给出决定。

        ``arguments`` 是**校验通过后**的参数 dict(loop 在 schema 校验之后
        才询问)——审批方看到的是工具真正要用的东西。
        """
        raise NotImplementedError


class DuplicateHookError(RuntimeError):
    """注册了同名的钩子。

    与 ``ToolRegistry`` 的 ``DuplicateToolError`` 同判据：
    静默覆盖/静默重复是最难排查的一类 bug，注册时就要暴露。
    """


class HookManager:
    """钩子注册表 + 事件派发（钩子总线）。

    **派发次序 = 注册次序**：同一个事件类型有多个订阅者时，
    谁先注册谁先收到。不提供优先级——需要顺序的钩子，
    注册顺序本身就是声明（显式优于隐式）。

    **零钩子 = 行为与没有钩子系统之前逐字节一致**（observer 同款承诺）。
    """

    def __init__(self) -> None:
        self._hooks: list[BaseHook] = []
        self._by_name: dict[str, BaseHook] = {}
        # 决策型钩子(审批)单独成名单:与通知型钩子同注册、同重名拒绝,
        # 但询问协议不同(要回传决定,不是单向通知)。
        self._approval_hooks: list[ApprovalHook] = []
        self._approval_by_name: dict[str, ApprovalHook] = {}

    def register(self, hook: BaseHook) -> None:
        """注册一个钩子。重名（含与类型默认名撞名）即抛。"""
        name = hook.name or type(hook).__name__
        if name in self._by_name:
            raise DuplicateHookError(
                f"钩子 {name!r} 已注册。同名钩子会让'这条记录是谁写的'无法追责，"
                "注册时就必须暴露（与 ToolRegistry 的重名拒绝同判据）。"
            )
        self._hooks.append(hook)
        self._by_name[name] = hook

    def subscribers(self, event_type: type[HookEvent]) -> list[BaseHook]:
        """某个钩子点（事件类型）当前的订阅者，按注册顺序。"""
        return [hook for hook in self._hooks if event_type in hook.events()]

    async def emit(self, event: HookEvent) -> None:
        """把事件派发给它的订阅者。

        异常**不吞**：钩子是行为扩展点，持久化类钩子失败还继续跑
        等于审计链分叉（Q2 拍板：宁可崩，不要错）。
        """
        for hook in self.subscribers(type(event)):
            outcome = hook.on_event(event)
            if inspect.isawaitable(outcome):
                await outcome

    def register_approval(self, hook: ApprovalHook) -> None:
        """注册一个审批钩子。重名(通知型/决策型两名单之间也不许撞)即抛。"""
        name = hook.name or type(hook).__name__
        if name in self._by_name or name in self._approval_by_name:
            raise DuplicateHookError(
                f"钩子 {name!r} 已注册。审批钩子与通知型钩子共用一个命名空间。"
            )
        self._approval_hooks.append(hook)
        self._approval_by_name[name] = hook

    async def approve_tool(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ApprovalDecision:
        """对一个即将执行的工具调用询问全部审批钩子。

        按注册顺序逐个询问,**任一拒绝 → 立即拒绝**(第一个拒绝理由生效);
        全部放行 → ``approve_outside`` 取并集(任一审批方明示豁免即豁免)。
        **没有注册任何审批钩子 → 默认放行**——与"零钩子 = 行为不变"
        同一条承诺:评测与子 agent 不注册审批,行为与没有 L3 之前一致。
        """
        approve_outside = False
        for hook in self._approval_hooks:
            decision = await hook.approve(name, arguments, call_id)
            if not decision.allowed:
                return decision
            approve_outside = approve_outside or decision.approve_outside
        return ApprovalDecision(allowed=True, approve_outside=approve_outside)

    def hook_names(self) -> list[str]:
        """已注册钩子名（观测与测试用）。"""
        return list(self._by_name)

    def approval_names(self) -> list[str]:
        """已注册审批钩子名（横幅与测试用）。"""
        return list(self._approval_by_name)
