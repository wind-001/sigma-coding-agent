"""钩子层：**唯一的行为与观测扩展面**——订阅者(BaseHook/ApprovalHook/AdvanceHook)与总线(HookManager)。

事件定义在 ``sigma/events/lifecycle.py``(P6 拆分:events 是机制,hooks 是订阅者)。
本模块**不认识**会话、持久化与 rich;``persist.py`` 是本包里唯一碰会话的成员。

**为什么是"唯一"扩展面**（P4-批次6，星辰拍板，推翻批次5 的"两通道"判据）
    钩子不仅控制 loop 走向（审批/拦截/推进），**日志记录并告知也是钩子的
    义务**。"渲染"不过是一个订阅了日志事件的钩子，与持久化钩子平级。

    接受的代价（写明不藏）：渲染路径依赖钩子注册——不注册渲染钩子就没有
    输出。这是特性：评测与子 agent 测试天然静默，CLI 注册了才有人看。

**三类决策/通知接口的分界**（推进机制批次 1 补上第三类）
    BaseHook       订阅者：发生了一件事,你看着办（单向通知）
    ApprovalHook   决策：这件事能不能发生（工具执行前,放行/拒绝）
    AdvanceHook    决策：这一轮还不算完,继续（模型收敛时,继续/停/不干预）
    三者注册进同一条总线、共用一个命名空间（重名即抛），各走询问协议。

**异常向上传播**（批次5 Q2 拍板）：持久化失败还继续跑 = 审计链分叉。
    **渲染类钩子自己负责宽容**（G34/G99：TerminalRenderer 内部降级），
    总线不代吞——宽容是订阅者的策略，不是总线的。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, Awaitable

from sigma.events.lifecycle import (
    AdvanceDecision,
    AdvanceFacts,
    ApprovalDecision,
    HookEvent,
)


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


class AdvanceHook(ABC):
    """**决策型**钩子第三类：推进——在模型没有请求的情况下，主动改变它
    下一轮能看到什么、**或是否还有下一轮**（CONTEXT.md「推进机制」词条）。

    与 :class:`ApprovalHook` 的分界：审批问"这件事能不能发生"（工具执行前，
    结果是放行/拒绝）；推进问"这一轮还不算完，继续"（模型收敛时，结果是
    继续一轮 / 要求优雅停止 / 不干预）。两者注册进**同一条总线**
    (HookManager)，但走各自的名单与询问协议——统一管理，职责分开。

    异常不吞（与审批通道同判据）：推进通道本身坏了应该暴露，
    而不是静默放行——静默放行等于把"继续"的决定权偷偷交回默认路径。
    """

    name: str

    @abstractmethod
    async def advance(self, facts: AdvanceFacts) -> AdvanceDecision:
        """对一次"模型想收手"给出决定。

        ``facts`` 是 loop 能搬的全部事实；"目标是否达成"的判据由实现方
        自取（构造时注入路径/命令）——loop 不替它决定看什么
        （计算型归代码：清单/验收命令；推断型归模型：要不要问用户）。
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
    这条承诺对三类钩子全境有效：不注册通知钩子不派发、不注册审批钩子
    默认放行（``approve_tool``）、不注册推进钩子默认不干预
    （``advance_decide`` 返回 None）——评测与子 agent 的静默是同款承诺。
    """

    def __init__(self) -> None:
        self._hooks: list[BaseHook] = []
        self._by_name: dict[str, BaseHook] = {}
        # 决策型钩子(审批)单独成名单:与通知型钩子同注册、同重名拒绝,
        # 但询问协议不同(要回传决定,不是单向通知)。
        self._approval_hooks: list[ApprovalHook] = []
        self._approval_by_name: dict[str, ApprovalHook] = {}
        # 决策型钩子(推进)第三个名单:与上两者同注册、同重名拒绝,
        # 询问协议是"这一轮还不算完,继续"(见 register_advance)。
        self._advance_hooks: list[AdvanceHook] = []
        self._advance_by_name: dict[str, AdvanceHook] = {}

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
        同一条承诺:评测与 sub_agent 不注册审批,行为与没有 L3 之前一致。
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

    def register_advance(self, hook: AdvanceHook) -> None:
        """注册一个推进钩子。重名(三个名单之间都不许撞)即抛。"""
        name = hook.name or type(hook).__name__
        if name in self._by_name or name in self._approval_by_name or name in self._advance_by_name:
            raise DuplicateHookError(
                f"钩子 {name!r} 已注册。推进钩子与通知型/审批型钩子共用一个命名空间。"
            )
        self._advance_hooks.append(hook)
        self._advance_by_name[name] = hook

    async def advance_decide(self, facts: AdvanceFacts) -> AdvanceDecision | None:
        """对一次"模型想收手"询问全部推进钩子。

        **没有注册任何推进钩子 → None**（= 不干预，行为与加它之前逐字节
        一致，与 ``approve_tool`` 的"零审批钩子默认放行"同一条承诺）。

        多钩子裁决（与审批"任一拒绝→拒绝"对偶，但停止权重高于继续——
        验收/安全类判据说不完，不许被"再试试"盖过）：

        1. 任一 ``force_stop`` → 取第一个的（连同理由）；
        2. 否则任一 ``continue_round`` → 继续，inject 取第一个要求继续的
           （多钩子注入打架比没有更坏）；
        3. 全部不干预 → None。
        """
        if not self._advance_hooks:
            return None
        decisions = [await hook.advance(facts) for hook in self._advance_hooks]
        for decision in decisions:
            if decision.force_stop:
                return decision
        for decision in decisions:
            if decision.continue_round:
                return decision
        return None

    def advance_names(self) -> list[str]:
        """已注册推进钩子名（横幅与测试用）。"""
        return list(self._advance_by_name)
