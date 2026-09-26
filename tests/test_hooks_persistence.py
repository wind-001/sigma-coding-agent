"""P4-批次5 的门槛测试：钩子事件驱动 + 会话树增量持久化 + 断点续跑。

每条测试对应详规里的一张门槛卡（G91–G97），
**每条都有"注入变红"的路径**——断言写的是时机与不变量，
实现一旦退化（挪回批量追加 / 漏发事件 / 双重追加）就当场红。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field

from sigma_agent.agent_messages import (
    AgentMessage,
    LlmMessageWrapper,
    ToolResultAgentMessage,
    convert_to_llm,
)
from sigma_agent.base import BaseTool
from sigma_agent.hooks import (
    AssistantProduced,
    BaseHook,
    DuplicateHookError,
    HookEvent,
    HookManager,
    MessageInjected,
    ToolEnd,
)
from sigma_agent.loop import AgentLoop
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.base import CancelToken
from sigma_ai.fake import FakeProvider
from sigma_ai.messages import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    Usage,
    UserMessage,
)
from sigma_session.context import SessionContext
from sigma_session.persist_hook import SessionPersistHook
from sigma_session.repair import repair_dangling_tool_results
from sigma_session.store import JsonlStore
from sigma_session.tree import SessionTree
from sigma.sdk import InteractiveSession

from sigma_ai.stamps import from_epoch as ts

FIXED_TIME = ts(1_700_000_000)


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 测试用工具与转录
# ---------------------------------------------------------------------------


class EchoParams(BaseModel):
    message: str = Field(description="要回显的内容")


class EchoTool(BaseTool):
    name = "echo"
    description = "回显 message"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text=f"echo: {cast(EchoParams, args).message}")])


def _tool_call_round(
    arguments: str, *, name: str = "echo", index: int = 0, call_id: str = "call_1"
) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": index,
            "id": call_id,
            "name": name,
            "arguments_delta": arguments,
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _make_context() -> SessionContext:
    """内存会话上下文（不落盘）：树用 SessionTree() 默认构造。"""
    return SessionContext(
        system_prompt="测试",
        tools_schema=[],
        clock=lambda: FIXED_TIME,
        tree=SessionTree(),
    )


def _make_loop(
    rounds: list[list[dict[str, Any]]],
    *,
    context: SessionContext,
    tools: list[BaseTool],
    hooks: HookManager | None,
) -> AgentLoop:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentLoop(
        provider=FakeProvider.from_rounds(rounds),
        registry=registry,
        model="fake",
        workspace_root=".",
        max_rounds=20,
        signal=_NeverCancelled(),
        clock=lambda: FIXED_TIME,
        hooks=hooks,
    )


def _assistant_messages(history: list[AgentMessage]) -> list[AssistantMessage]:
    return [
        message.message
        for message in history
        if isinstance(message, LlmMessageWrapper)
        and isinstance(message.message, AssistantMessage)
    ]


def _tool_results(history: list[AgentMessage]) -> list[ToolResultAgentMessage]:
    return [m for m in history if isinstance(m, ToolResultAgentMessage)]


# ---------------------------------------------------------------------------
# G91：LLM 返回即持久化——工具执行时树上已有本轮 assistant 节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assistant_persisted_before_tools_run() -> None:
    """靶工具在 run() 里查树：此时 assistant 必须已经落进历史。

    注入：把 AssistantProduced 的 emit 挪回 run_turn 结束 → 本条红
    （工具执行时历史里一条 assistant 都没有）。
    """
    context = _make_context()
    seen_assistants_at_run: list[int] = []

    class Probe(EchoTool):
        name = "probe"

        async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
            seen_assistants_at_run.append(len(_assistant_messages(context.history())))
            return ToolResult(content=[TextBlock(text="ok")])

    hooks = HookManager()
    hooks.register(SessionPersistHook(context))
    loop = _make_loop(
        [_tool_call_round('{"message": "hi"}', name="probe"), _text_round("完成")],
        context=context,
        tools=[Probe()],
        hooks=hooks,
    )

    await loop.run_turn(
        [
            LlmMessageWrapper(
                timestamp=FIXED_TIME,
                message=UserMessage(content="干活", timestamp=FIXED_TIME),
            )
        ]
    )

    assert seen_assistants_at_run == [1]


# ---------------------------------------------------------------------------
# G92：每个工具结果独立节点；钩子异常传播时，早前的结果已在树上
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_tool_result_is_its_own_node() -> None:
    """一批两个调用 → 两个独立的结果节点（G92 的最终状态断言）。"""
    context = _make_context()
    hooks = HookManager()
    hooks.register(SessionPersistHook(context))
    loop = _make_loop(
        [
            _tool_call_round('{"message": "a"}', call_id="call_1", index=0),
            _tool_call_round('{"message": "b"}', call_id="call_2", index=1),
            _text_round("完成"),
        ],
        context=context,
        tools=[EchoTool()],
        hooks=hooks,
    )

    await loop.run_turn([])

    results = _tool_results(context.history())
    assert [r.tool_call_id for r in results] == ["call_1", "call_2"]


@pytest.mark.asyncio
async def test_persist_failure_keeps_earlier_results() -> None:
    """钩子在第二条结果时抛异常 → 本轮中止，但**第一条结果已在树上**。

    这是 G92 的时机断言：如果是"批次结束再批量追加"，异常时树上一条
    结果都没有——只有逐条落盘才能保住第一条。
    """
    context = _make_context()

    class BoomOnSecond(BaseHook):
        name = "boom-on-second"
        _count = 0

        def events(self) -> tuple[type[HookEvent], ...]:
            return (ToolEnd,)

        def on_event(self, event: HookEvent) -> None:
            type(self)._count += 1
            if type(self)._count >= 2:
                raise RuntimeError("第二条结果时故意炸")

    # 故障钩子注册在持久化钩子**之前**：第二条结果的事件在落盘前就被
    # 它炸掉——这样"树上只有第一条"才证明逐事件落盘的时机
    # （若实现退化成批次结束再批量追加，异常时树上一条结果都没有）。
    hooks = HookManager()
    hooks.register(BoomOnSecond())
    hooks.register(SessionPersistHook(context))
    loop = _make_loop(
        [
            _tool_call_round('{"message": "a"}', call_id="call_1", index=0),
            _tool_call_round('{"message": "b"}', call_id="call_2", index=1),
        ],
        context=context,
        tools=[EchoTool()],
        hooks=hooks,
    )

    with pytest.raises(RuntimeError, match="第二条结果"):
        await loop.run_turn([])

    results = _tool_results(context.history())
    assert [r.tool_call_id for r in results] == ["call_1"]


# ---------------------------------------------------------------------------
# G93：注入类消息（steering / 信箱）注入即落盘
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mailbox_message_persisted_at_drain() -> None:
    """信箱回报在 drain 注入的那一刻就落盘（drain-once，不落即丢）。"""
    context = _make_context()
    hooks = HookManager()
    hooks.register(SessionPersistHook(context))

    drained: list[bool] = []

    def drain() -> list[AgentMessage]:
        if drained:
            return []
        drained.append(True)
        return [
            LlmMessageWrapper(
                timestamp=FIXED_TIME,
                message=UserMessage(content="子任务回报：完成", timestamp=FIXED_TIME),
            )
        ]

    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = AgentLoop(
        provider=FakeProvider.from_rounds([_text_round("收到")]),
        registry=registry,
        model="fake",
        workspace_root=".",
        max_rounds=20,
        signal=_NeverCancelled(),
        clock=lambda: FIXED_TIME,
        hooks=hooks,
        mailbox_drain=drain,
    )

    await loop.run_turn([])

    injected = [
        m
        for m in context.history()
        if isinstance(m, LlmMessageWrapper)
        and isinstance(m.message, UserMessage)
        and "子任务回报" in str(m.message.content)
    ]
    assert len(injected) == 1


# ---------------------------------------------------------------------------
# G94：不双重追加——send 后树上恰好是"逐事件"那条线
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_does_not_double_append(tmp_path: Path) -> None:
    """send 后节点数 == 逐事件计数；send() 若恢复末尾批量 append 则翻倍变红。"""
    session = InteractiveSession(
        provider=FakeProvider.from_rounds(
            [
                _tool_call_round('{"message": "hi"}', call_id="call_1"),
                _text_round("完成"),
            ]
        ),
        workspace_root=tmp_path,
        model="fake",
        registry=_registry_with_echo(),
        system_prompt="测试",
        max_rounds=20,
        enable_compaction=False,
        enable_todo=False,
    )

    await session.send("干活")

    # 1 user + 1 assistant + 1 tool_result + 1 final assistant
    assert len(session.context.tree) == 4


# ---------------------------------------------------------------------------
# G95 / G96：断点续跑——悬空 tool_call 修复
# ---------------------------------------------------------------------------


def _assistant_with_calls(*call_ids: str) -> LlmMessageWrapper:
    return LlmMessageWrapper(
        timestamp=FIXED_TIME,
        message=AssistantMessage(
            content=[
                ToolCallBlock(id=call_id, name="echo", arguments={"message": call_id})
                for call_id in call_ids
            ],
            usage=Usage(prompt_tokens=1, completion_tokens=1),
            stop_reason="tool_use",
            timestamp=FIXED_TIME,
        ),
    )


def _registry_with_echo() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def test_repair_synthesizes_missing_results(tmp_path: Path) -> None:
    """assistant(a,b) + result(a) 的中断现场：续跑会话自动补 result(b)。

    注入：删掉 __init__ 里的 repair 调用 → 树上没有 b 的结果，
    且下一轮请求体缺 b 的 tool_result（协议 400）→ 本条红。
    """
    store = JsonlStore(tmp_path, "repair-1")
    crashed = SessionTree(store=store)
    crashed.append(
        LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="干活", timestamp=FIXED_TIME),
        )
    )
    crashed.append(_assistant_with_calls("call_a", "call_b"))
    crashed.append(
        ToolResultAgentMessage(
            tool_call_id="call_a",
            tool_name="echo",
            content=[TextBlock(text="echo: a")],
            timestamp=FIXED_TIME,
        )
    )
    assert len(crashed) == 3

    session = InteractiveSession(
        provider=FakeProvider.from_rounds([_text_round("继续")]),
        workspace_root=tmp_path,
        model="fake",
        registry=_registry_with_echo(),
        system_prompt="测试",
        max_rounds=20,
        enable_compaction=False,
        enable_todo=False,
        tree=SessionTree.from_store(store),
    )

    # 修复节点已落盘（持久化，可审计）
    assert len(session.context.tree) == 4
    repaired = _tool_results(session.context.history())
    assert [r.tool_call_id for r in repaired] == ["call_a", "call_b"]
    assert repaired[1].is_error is True
    assert repaired[1].details.get("repaired") is True

    # 下一轮发给模型的消息序列协议完整：a、b 都有 tool_result
    llm_messages = convert_to_llm(session.context.build_messages())
    tool_result_ids = [
        m.tool_call_id for m in llm_messages if m.role == "tool_result"
    ]
    assert sorted(tool_result_ids) == ["call_a", "call_b"]


def test_repair_is_idempotent(tmp_path: Path) -> None:
    """二次加载零新增：修复节点落盘后悬空即消失（G96）。"""
    store = JsonlStore(tmp_path, "repair-2")
    tree = SessionTree(store=store)
    tree.append(_assistant_with_calls("call_a"))

    first = repair_dangling_tool_results(tree, clock=lambda: FIXED_TIME)
    second = repair_dangling_tool_results(tree, clock=lambda: FIXED_TIME)

    assert len(first) == 1
    assert second == []
    assert len(tree) == 2


def test_healthy_session_is_untouched(tmp_path: Path) -> None:
    """健康会话（无悬空）：修复是纯读扫描，零写入零副作用。"""
    store = JsonlStore(tmp_path, "repair-3")
    tree = SessionTree(store=store)
    tree.append(_assistant_with_calls("call_a"))
    tree.append(
        ToolResultAgentMessage(
            tool_call_id="call_a",
            tool_name="echo",
            content=[TextBlock(text="echo: a")],
            timestamp=FIXED_TIME,
        )
    )

    assert repair_dangling_tool_results(tree, clock=lambda: FIXED_TIME) == []
    assert len(tree) == 2


# ---------------------------------------------------------------------------
# G97：HookManager 本体——重名拒绝、按事件类型派发、注册顺序
# ---------------------------------------------------------------------------


class _RecordingHook(BaseHook):
    def __init__(
        self,
        name: str,
        *event_types: type[HookEvent],
        on_tool_result: Any = None,
    ) -> None:
        self.name = name
        self._event_types = event_types
        self.seen: list[str] = []

    def events(self) -> tuple[type[HookEvent], ...]:
        return self._event_types

    def on_event(self, event: HookEvent) -> None:
        self.seen.append(type(event).__name__)


def test_duplicate_hook_name_rejected() -> None:
    manager = HookManager()
    manager.register(_RecordingHook("same", AssistantProduced))
    with pytest.raises(DuplicateHookError):
        manager.register(_RecordingHook("same", AssistantProduced))


@pytest.mark.asyncio
async def test_events_dispatch_only_to_subscribers() -> None:
    manager = HookManager()
    assistant_only = _RecordingHook("a", AssistantProduced)
    tool_only = _RecordingHook("t", ToolEnd)
    manager.register(assistant_only)
    manager.register(tool_only)

    await manager.emit(AssistantProduced(message=LlmMessageWrapper(
        timestamp=FIXED_TIME,
        message=AssistantMessage(
            content=[],
            usage=Usage(prompt_tokens=0, completion_tokens=0),
            stop_reason="stop",
            timestamp=FIXED_TIME,
        ),
    )))
    await manager.emit(
        ToolEnd(
            name="echo",
            ok=True,
            preview="echo: x",
            message=ToolResultAgentMessage(
                tool_call_id="c1",
                tool_name="echo",
                content=[TextBlock(text="x")],
                timestamp=FIXED_TIME,
            ),
        )
    )
    await manager.emit(MessageInjected(
        message=LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="提醒", timestamp=FIXED_TIME),
        )
    ))

    assert assistant_only.seen == ["AssistantProduced"]
    assert tool_only.seen == ["ToolEnd"]


@pytest.mark.asyncio
async def test_dispatch_order_is_registration_order() -> None:
    manager = HookManager()
    first = _RecordingHook("first", MessageInjected)
    second = _RecordingHook("second", MessageInjected)
    manager.register(first)
    manager.register(second)

    await manager.emit(MessageInjected(
        message=LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="提醒", timestamp=FIXED_TIME),
        )
    ))

    assert first.seen == ["MessageInjected"]
    assert second.seen == ["MessageInjected"]


@pytest.mark.asyncio
async def test_async_hook_supported() -> None:
    class AsyncHook(BaseHook):
        name = "async-hook"

        def __init__(self) -> None:
            self.called = False

        def events(self) -> tuple[type[HookEvent], ...]:
            return (MessageInjected,)

        async def on_event(self, event: HookEvent) -> None:
            self.called = True

    hook = AsyncHook()
    manager = HookManager()
    manager.register(hook)
    await manager.emit(MessageInjected(
        message=LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="提醒", timestamp=FIXED_TIME),
        )
    ))
    assert hook.called is True
