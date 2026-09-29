"""钩子层：**唯一的行为与观测扩展面**——订阅者(BaseHook/ApprovalHook)与总线(HookManager)。

事件定义在 ``sigma/events/lifecycle.py``(P6 拆分:events 是机制,hooks 是订阅者)。
本模块**不认识**会话、持久化与 rich;``persist.py`` 是本包里唯一碰会话的成员。

**为什么是"唯一"扩展面**（P4-批次6，星辰拍板，推翻批次5 的"两通道"判据）
    钩子不仅控制 loop 走向（将来的审批/拦截），**日志记录并告知也是钩子的
    义务**。"渲染"不过是一个订阅了日志事件的钩子，与持久化钩子平级。

    接受的代价（写明不藏）：渲染路径依赖钩子注册——不注册渲染钩子就没有
    输出。这是特性：评测与子 agent 测试天然静默，CLI 注册了才有人看。

**异常向上传播**（批次5 Q2 拍板）：持久化失败还继续跑 = 审计链分叉。
    **渲染类钩子自己负责宽容**（G34/G99：TerminalRenderer 内部降级），
    总线不代吞——宽容是订阅者的策略，不是总线的。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, Awaitable

from sigma.events.lifecycle import ApprovalDecision, HookEvent


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
