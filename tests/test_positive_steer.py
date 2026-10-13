"""正向推进注入（推进机制批次 4）的测试。

判据（GA-3 注入实验证伪，见 scripts/gate_injection_advance.py）：
    G-PS-1  默认 interval=0 → 一次都不注入（零值即不变）；
    G-PS-2  连续 N 轮无推进信号（read/grep/task）→ 注入并清零，周期可重复；
    G-PS-3  推进信号轮清零（read/grep/task 任一都算）；
    G-PS-3' todo **不**算推进信号（规划不产生新证据）；
    G-PS-4  文案按注册表现状拼：没有 task 工具就不提"派子 agent"
            （工具行与注册表同源的 loop 侧版本——否则模型白调一次）；
    G-PS-5  loop 集成：注入走 MessageInjected（尾部 user 消息、动态区），
            轮次与文案都可断言。

时序模型（读断言前必读，2026-10-13 第一版测试就在这里推错）：
    - 注入点在**每轮开头**（provider 调用之前）：本轮模型就能看到；
    - 计数器在工具批次执行后更新（``_count_positive_touch``），
      纯文本轮（``not calls``）同样 +1——只说话不查证也是空转；
    - 所以 interval=2 时：r1/r2 两轮无信号 → r3 开头才注入，不是 r2。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field

from sigma.agent.types import ToolContext, ToolResult
from sigma.events.lifecycle import MessageInjected
from sigma.hooks.base import BaseHook, HookEvent, HookManager
from sigma.providers.base import NeverCancelled
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock
from sigma.providers.stamps import from_epoch as ts
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.base import BaseTool
from sigma.tools.registry import ToolRegistry

FIXED_TIME = ts(1_700_000_000)

# 推进信号工具名（loop 侧判据只认名字，与 ``_count_positive_touch`` 同源）。
# 注册它们不是为了真执行什么，是为了让"有查证工具可提"这个前提成立——
# 没有 read/grep 的实现分支是"没有查证工具可提就不注入"（G-PS 的防御）。
_PROGRESS_TOOLS = ("read", "grep")


class _AnyParams(BaseModel):
    """宽松参数模型：read/grep/task/todo 的假实现共用，任意子集都能过校验。"""

    message: str = Field(default="", description="消息")
    path: str = Field(default="", description="路径")
    pattern: str = Field(default="", description="模式")
    action: str = Field(default="", description="动作")
    prompt: str = Field(default="", description="提示")


class _EchoTool(BaseTool):
    """echo 不是推进信号（不读文件、不搜索、不派发）——测试用它占轮次。"""

    name = "echo"
    description = "回显"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _AnyParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params: _AnyParams = args  # type: ignore[assignment]
        return ToolResult(content=[TextBlock(text=f"echo: {params.message}")])


class _NamedTool(BaseTool):
    """按名字注册的假工具（read/grep/task/todo）——只为计数与文案服务。"""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._name

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"假 {self._name}"

    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _AnyParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text=f"{self._name}: ok")])


class _InjectionRecorder(BaseHook):
    """只录 MessageInjected——正向注入的断言面。"""

    name = "inj-rec"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def events(self) -> tuple[type[HookEvent], ...]:
        return (MessageInjected,)

    def on_event(self, event: HookEvent) -> None:
        assert isinstance(event, MessageInjected)
        content = getattr(event.message, "message", None)
        text = getattr(content, "content", "")
        self.seen.append(str(text))


def _tool_call_round(
    arguments: str = '{"message": "hi"}', *, name: str = "echo"
) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": "call_1",
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


def _make_loop(
    rounds: list[list[dict[str, Any]]],
    *,
    positive_steer_interval: int = 0,
    recorder: _InjectionRecorder | None = None,
    extra_tools: list[str] | None = None,
) -> AgentLoop:
    """默认注册表：echo（非推进信号）+ read/grep（推进信号 + 文案前提）。

    echo 每轮换参数：相同的调用与结果会触发护栏停滞软检测的 nudge
    （另一条 MessageInjected），与本测试无关——换参数把它彻底排除。
    """
    registry = ToolRegistry()
    registry.register(_EchoTool())
    for tool_name in _PROGRESS_TOOLS:
        registry.register(_NamedTool(tool_name))
    for tool_name in extra_tools or []:
        registry.register(_NamedTool(tool_name))
    hooks = None
    if recorder is not None:
        hooks = HookManager()
        hooks.register(recorder)
    return AgentLoop(
        provider=FakeProvider.from_rounds(rounds),
        registry=registry,
        model="fake",
        workspace_root=".",
        signal=NeverCancelled(),
        clock=lambda: FIXED_TIME,
        hooks=hooks,
        positive_steer_interval=positive_steer_interval,
    )


def _steer_texts(recorder: _InjectionRecorder) -> list[str]:
    """只留正向推进注入（滤掉护栏 nudge 等其他 MessageInjected）。"""
    return [t for t in recorder.seen if "推进提醒" in t]


# ---------------------------------------------------------------------------
# loop 集成（计数 + 文案 + 注入时序，经 provider 完整跑一轮）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_interval_never_injects() -> None:
    """G-PS-1：默认 0 → 一次都不注入（零值即不变）。"""
    recorder = _InjectionRecorder()
    loop = _make_loop(
        [
            _tool_call_round(arguments='{"message": "a"}'),
            _tool_call_round(arguments='{"message": "b"}'),
            _text_round("完成"),
        ],
        recorder=recorder,
    )
    result = await loop.run_turn([])
    assert result.status == "completed"
    assert recorder.seen == []
    # 两轮 echo + 一轮纯文本，都不是推进信号——计数器照常计（只是不注入）
    assert loop.positive_stall == 3


@pytest.mark.asyncio
async def test_injects_after_n_rounds_without_progress() -> None:
    """G-PS-2：interval=2 → 连续 2 轮无推进信号后注入，清零后可再次触发。"""
    recorder = _InjectionRecorder()
    loop = _make_loop(
        [
            _tool_call_round(arguments='{"message": "a"}'),  # r1：echo → stall 1
            _tool_call_round(arguments='{"message": "b"}'),  # r2：echo → stall 2
            _tool_call_round(arguments='{"message": "c"}'),  # r3 开头注入#1 → 清零 → echo → 1
            _tool_call_round(arguments='{"message": "d"}'),  # r4：echo → stall 2
            _text_round("完成"),  # r5 开头注入#2 → 清零 → 文本收尾 → stall 1
        ],
        positive_steer_interval=2,
        recorder=recorder,
    )
    result = await loop.run_turn([])
    assert result.status == "completed"
    injected = _steer_texts(recorder)
    assert len(injected) == 2
    assert all("连续 2 轮" in t for t in injected)
    # 注入后计数器清零，周期可以重复（不是一次性开关）
    assert loop.positive_stall == 1


@pytest.mark.asyncio
async def test_read_clears_the_counter() -> None:
    """G-PS-3：read 是推进信号 → 清零（不清零的话 r5 开头会再注入一次）。"""
    recorder = _InjectionRecorder()
    loop = _make_loop(
        [
            _tool_call_round(arguments='{"message": "a"}'),  # r1：echo → stall 1
            _tool_call_round(arguments='{"message": "b"}'),  # r2：echo → stall 2
            _tool_call_round(  # r3 开头注入#1 → 清零 → read → stall 0
                arguments='{"path": "x"}', name="read"
            ),
            _tool_call_round(arguments='{"message": "c"}'),  # r4：echo → stall 1
            _text_round("完成"),  # r5 开头 stall 1 < 2 → 不注入
        ],
        positive_steer_interval=2,
        recorder=recorder,
    )
    result = await loop.run_turn([])
    assert result.status == "completed"
    injected = _steer_texts(recorder)
    # 只有 r3 开头那一次：read 把计数器按住，之后再没攒到 2
    assert len(injected) == 1


@pytest.mark.asyncio
async def test_grep_clears_the_counter() -> None:
    """G-PS-3：grep 同为推进信号 → 清零（与 read 同一条判据的另一半）。"""
    recorder = _InjectionRecorder()
    loop = _make_loop(
        [
            _tool_call_round(arguments='{"message": "a"}'),
            _tool_call_round(arguments='{"message": "b"}'),
            _tool_call_round(  # r3 开头注入#1 → 清零 → grep → stall 0
                arguments='{"pattern": "x"}', name="grep"
            ),
            _tool_call_round(arguments='{"message": "c"}'),
            _text_round("完成"),
        ],
        positive_steer_interval=2,
        recorder=recorder,
    )
    result = await loop.run_turn([])
    assert result.status == "completed"
    assert len(_steer_texts(recorder)) == 1


@pytest.mark.asyncio
async def test_todo_does_not_clear_the_counter() -> None:
    """G-PS-3'：todo 不算推进信号（规划不产生新证据）。

    序列 [echo, todo, echo, text]：若 todo 也算信号，计数器永远攒不到 2
    （0 次注入）；实现判它不算 → r3 开头恰好注入一次。
    """
    recorder = _InjectionRecorder()
    loop = _make_loop(
        [
            _tool_call_round(arguments='{"message": "a"}'),  # r1：echo → stall 1
            _tool_call_round(  # r2：todo → stall 2（todo 不清零）
                arguments='{"action": "list"}', name="todo"
            ),
            _tool_call_round(arguments='{"message": "c"}'),  # r3 开头注入#1 → 清零 → echo → 1
            _text_round("完成"),
        ],
        positive_steer_interval=2,
        recorder=recorder,
        extra_tools=["todo"],
    )
    result = await loop.run_turn([])
    assert result.status == "completed"
    assert len(_steer_texts(recorder)) == 1


@pytest.mark.asyncio
async def test_text_without_task_tool_omits_dispatch_hint() -> None:
    """G-PS-4：注册表没有 task → 文案不提"派子 agent"（同源纪律）。"""
    recorder = _InjectionRecorder()
    loop = _make_loop(
        [
            _tool_call_round(arguments='{"message": "a"}'),
            _tool_call_round(arguments='{"message": "b"}'),
            _text_round("完成"),  # r3 开头注入#1
        ],
        positive_steer_interval=2,
        recorder=recorder,
    )
    result = await loop.run_turn([])
    assert result.status == "completed"
    injected = _steer_texts(recorder)
    assert len(injected) == 1
    assert "连续 2 轮" in injected[0]
    assert "read/grep" in injected[0]
    assert "派子 agent" not in injected[0]


@pytest.mark.asyncio
async def test_with_task_tool_mentions_dispatch_hint() -> None:
    """G-PS-4 另一面：注册表有 task → 文案带"派子 agent"提示。"""
    recorder = _InjectionRecorder()
    loop = _make_loop(
        [
            _tool_call_round(arguments='{"message": "a"}'),
            _tool_call_round(arguments='{"message": "b"}'),
            _text_round("完成"),
        ],
        positive_steer_interval=2,
        recorder=recorder,
        extra_tools=["task"],
    )
    result = await loop.run_turn([])
    assert result.status == "completed"
    injected = _steer_texts(recorder)
    assert len(injected) == 1
    assert "派子 agent" in injected[0]
