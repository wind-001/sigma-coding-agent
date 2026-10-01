"""会话树持久化钩子：把"追加历史"从 send 末尾的一次 API 调用降格为事件订阅者。

为什么住在本层（sigma.sessions）
    它消费 :class:`SessionContext`——追加历史的知识在这里；
    而 ``BaseHook`` / 事件类型在 sigma.agent，``sigma.sessions → sigma.agent``
    是允许的依赖方向。产品壳（sdk）只负责把两者**接线**：
    构造本钩子并 ``register`` 进 HookManager。

粒度（P4-批次5 拍板；P4-批次6 起结果事件更名为 ``ToolEnd``）
    订阅全部三个钩子点，每个事件 append 一次——于是
    **每一次 LLM 返回、每一个工具结果落定、每一次注入**都立即写穿到
    会话树（``SessionTree._commit`` 先落盘再改内存）。
    中断时，树上永远停在最后一条已发生的消息上，续跑从那里开始。

为什么三个点用同一个动作
    三个时机对持久化来说语义相同："这条消息已进入历史"。
    区分三个**事件类型**是为了让别的钩子能只订阅其中一个时机
    （比如将来的审批钩子只关心工具结果），不是为了本钩子分支处理。
"""

from __future__ import annotations

from sigma.events.lifecycle import (
    AssistantProduced,
    HookEvent,
    MessageInjected,
    ToolEnd,
)
from sigma.hooks.base import (
    BaseHook,
)

from sigma.sessions.context import SessionContext


class SessionPersistHook(BaseHook):
    """订阅全部产出事件，把载荷消息逐条追加进 SessionContext。"""

    name = "session-persist"

    def __init__(self, context: SessionContext) -> None:
        self._context = context

    @property
    def context(self) -> SessionContext:
        """钩子当前绑定的 context。热重载 rebind 的接线必须**可被从外部断言**
        （G-HR-3）——私有属性一改名测试就失效，与 ``resident_text()`` 同判据。"""
        return self._context

    def rebind(self, context: SessionContext) -> None:
        """把本钩子接到（热重载重建后的）新 context 上。

        新旧 context **共享同一棵树**，所以不 rebind 持久化也"能写"——
        但钩子持有的必须是**当前在用的**那个 context：持有旧引用，
        将来任何经 context 的读取（压缩视图 / 指纹）读到的都是被废弃的实例，
        而症状离根因极远。接线纪律：不留旧引用。
        """
        self._context = context

    def events(self) -> tuple[type[HookEvent], ...]:
        return (AssistantProduced, ToolEnd, MessageInjected)

    def on_event(self, event: HookEvent) -> None:
        # append 内部逐条走 tree.append（先落盘再改内存），
        # 落盘失败会从这里抛出去、经总线传播中止本轮（批次5 Q2 拍板）。
        # isinstance 是给 mypy 的收窄：总线保证只派发 events() 里声明过的
        # 三类（它们都带 message）；其余钩子点到了这里就是接线写错了，
        # 静默返回与"不订阅"语义一致。
        if not isinstance(event, (AssistantProduced, ToolEnd, MessageInjected)):
            return
        self._context.append(event.message)
