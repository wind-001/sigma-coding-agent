"""agent loop 的端到端离线测试。

**这一批用例是"跑通"的实际证明**：喂一段录制的模型输出，让 loop 真的
调用工具、把结果写回上下文、再问一轮，最后断言产出。

它的价值不在"测试覆盖率"，而在：**在没有 API key 的情况下，把 loop 的
控制流每一处分支都走一遍**。这是架构 7.2 节「确定性回放」的落地，
也是门槛 G27 / G28 / G30 / G31 的验证处。

时间戳通过 ``clock`` 注入固定值——否则"两次执行结果一致"永远不成立。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import BaseModel, Field

from sigma_agent.agent_messages import AgentMessage, LlmMessageWrapper
from sigma_agent.base import BaseLoop, BaseTool
from sigma_agent.loop import AgentLoop
from sigma_agent.registry import DuplicateToolError, ToolRegistry
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.base import CancelToken
from sigma_ai.fake import FakeProvider, TranscriptExhausted
from sigma_ai.messages import TextBlock, UserMessage

FIXED_TIME = 1_700_000_000


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 测试用工具
# ---------------------------------------------------------------------------


class EchoParams(BaseModel):
    """参数模型。``message`` 是必填——用于验证 schema 校验。"""

    message: str = Field(description="要回显的内容")


class EchoTool(BaseTool):
    """只读的回显工具。记录每次调用的参数，供测试断言。"""

    name = "echo"
    description = "回显 message"
    read_only = True

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(EchoParams, args)
        self.seen.append(params.message)
        return ToolResult(content=[TextBlock(text=f"echo: {params.message}")])


class ExplodingTool(BaseTool):
    """**故意抛异常**的工具，用来验证 loop 的兜底层。"""

    name = "boom"
    description = "总是抛异常"
    read_only = False

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("工具自己没接住这个异常")


class FailingTool(BaseTool):
    """**按约定返回 is_error** 的工具（正确姿势）。"""

    name = "failing"
    description = "总是失败但按约定返回"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            content=[TextBlock(text="stderr: 命令返回码 1")],
            details={"exit_code": 1},
            is_error=True,
        )


def _make_loop(
    rounds: list[list[dict[str, Any]]],
    *,
    tools: list[BaseTool] | None = None,
    max_rounds: int = 20,
) -> tuple[AgentLoop, FakeProvider, ToolRegistry]:
    registry = ToolRegistry()
    for tool in tools if tools is not None else [EchoTool()]:
        registry.register(tool)

    provider = FakeProvider.from_rounds(rounds)
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        model="fake",
        workspace_root=".",
        max_rounds=max_rounds,
        signal=_NeverCancelled(),
        clock=lambda: FIXED_TIME,
    )
    return loop, provider, registry


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


def _history() -> list[AgentMessage]:
    """构造历史消息。

    ⚠️ **必须是 agent 层消息**（``LlmMessageWrapper``），不是裸的 ``UserMessage``。

    这个坑我第一次就踩了：传 ``UserMessage`` 进去，``convert_to_llm``
    认不出它（LLM 层消息没有 ``to_llm()``），于是**丢弃 + 一条 warning**。
    测试仍然全绿——因为 loop 把"历史为空"也当作合法输入跑完了。

    **这正是批次 1.5 的 W1 决策（warning + 丢弃，而不是静默）的第一次实战收益**：
    如果照抄 Pi 的静默丢弃，我会得到一个"loop 看起来正常、但模型从没看到历史"
    的假通过。见详规附录 A 对 S6 的记录。
    """
    return [
        LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="请回显 hi", timestamp=FIXED_TIME),
        )
    ]


# ---------------------------------------------------------------------------
# 正常路径：这是"跑通"的正面证据
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_tool_call_then_complete() -> None:
    """一轮工具调用 + 一轮文本收尾。"""
    tool = EchoTool()
    loop, provider, _ = _make_loop(
        [_tool_call_round('{"message": "hi"}'), _text_round("完成")],
        tools=[tool],
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    assert result.rounds == 2
    assert result.text == "完成"
    # transcript 被完整消费
    assert provider.remaining_rounds == 0
    # 工具真的被调用了，且参数经过 Pydantic 校验
    assert tool.seen == ["hi"]
    # 产出 = assistant + tool_result + assistant
    assert len(result.messages) == 3


@pytest.mark.asyncio
async def test_arguments_split_across_deltas_are_concatenated() -> None:
    """arguments 跨多个 delta 分片到达时能正确拼装。

    真实 API 上就是这样（冒烟 P4 探测实测到 16 个分片）。
    按到达顺序硬拼在单工具时也能过，所以这条用例的真正价值在下一个
    多 index 交错的场景里。
    """
    tool = EchoTool()
    fragments = ['{"mess', 'age": ', '"分片"}']
    round_one: list[dict[str, Any]] = [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": "call_x",
            "name": "echo",
            "arguments_delta": frag,
        }
        for frag in fragments
    ]
    round_one.append({"type": "stop", "stop_reason": "tool_use"})

    loop, _, _ = _make_loop([round_one, _text_round("ok")], tools=[tool])
    result = await loop.run_turn(_history())

    assert result.status == "completed"
    assert tool.seen == ["分片"]


@pytest.mark.asyncio
async def test_two_tool_calls_in_one_round_keep_order() -> None:
    """单轮多个工具调用：**结果顺序必须与调用顺序一致**（门槛 G27）。

    两个 index 交错到达——这正是"按到达顺序硬拼"会出错的形态。
    """
    tool = EchoTool()
    round_one: list[dict[str, Any]] = [
        # index 0 与 index 1 交错，模拟真实的多工具并行
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": "call_a",
            "name": "echo",
            "arguments_delta": '{"message": "first"}',
        },
        {
            "type": "tool_call_delta",
            "index": 1,
            "id": "call_b",
            "name": "echo",
            "arguments_delta": '{"message": "second"}',
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]

    loop, _, _ = _make_loop([round_one, _text_round("done")], tools=[tool])
    result = await loop.run_turn(_history())

    assert result.status == "completed"
    # 两次调用都到了，且**顺序是 index 0 → 1**，不是到达顺序的反面
    assert tool.seen == ["first", "second"]

    # 结果消息的 tool_call_id 必须与调用的 id 对应，不能错配
    from sigma_agent.agent_messages import ToolResultAgentMessage

    tool_results = [m for m in result.messages if isinstance(m, ToolResultAgentMessage)]
    assert [m.tool_call_id for m in tool_results] == ["call_a", "call_b"]
    assert [m.tool_name for m in tool_results] == ["echo", "echo"]


# ---------------------------------------------------------------------------
# 失败路径：全部必须"变成模型可见的信息"，而不是异常
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_error_is_visible_and_loop_continues() -> None:
    """工具按约定返回 ``is_error`` → 结果进上下文，且 **loop 继续**（门槛 G28）。

    这是「纠错增益」指标能被观测到的实现基础：
    如果 loop 在第一次失败就 return，那个指标永远是 0。
    """
    loop, _, _ = _make_loop(
        [_tool_call_round('{"message": "x"}', name="failing"), _text_round("我看到失败了")],
        tools=[FailingTool()],
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    assert result.rounds == 2  # ← 关键：没有在失败处停下

    from sigma_agent.agent_messages import ToolResultAgentMessage

    tool_results = [m for m in result.messages if isinstance(m, ToolResultAgentMessage)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    # stderr 必须进 content，不能只进 details —— 否则模型看不见就无从纠错
    assert "stderr" in tool_results[0].content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_tool_raising_exception_is_caught_by_loop() -> None:
    """工具**违约抛异常**时，loop 兜底转成 is_error，不让异常穿透。"""
    loop, _, _ = _make_loop(
        [_tool_call_round('{"message": "x"}', name="boom"), _text_round("收到")],
        tools=[ExplodingTool()],
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    from sigma_agent.agent_messages import ToolResultAgentMessage

    tool_results = [m for m in result.messages if isinstance(m, ToolResultAgentMessage)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert "工具自己没接住这个异常" in tool_results[0].content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_invalid_json_arguments_becomes_visible_error() -> None:
    """arguments 不是合法 JSON → 一级校验失败，结果给模型看，不抛异常。"""
    loop, _, _ = _make_loop(
        [_tool_call_round('{"message": "unterminated'), _text_round("我改")],
        tools=[EchoTool()],
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    # 工具**没有**被执行
    from sigma_agent.agent_messages import ToolResultAgentMessage

    tool_results = [m for m in result.messages if isinstance(m, ToolResultAgentMessage)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert "不是合法 JSON" in tool_results[0].content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_schema_violation_becomes_visible_error() -> None:
    """JSON 合法但不符合 schema → **二级校验**失败（门槛 G31）。

    这一条对应「参照 Pi 补进来的一步」：原设计只有 JSON 层校验。
    """
    tool = EchoTool()
    # 合法 JSON，但缺少必填的 message 字段
    loop, _, _ = _make_loop(
        [_tool_call_round('{"wrong_field": 1}'), _text_round("我改")],
        tools=[tool],
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    # **工具没有被执行**——校验失败就不该执行
    assert tool.seen == []

    from sigma_agent.agent_messages import ToolResultAgentMessage

    tool_results = [m for m in result.messages if isinstance(m, ToolResultAgentMessage)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    text = tool_results[0].content[0].text  # type: ignore[union-attr]
    assert "不符合 schema" in text
    # 错误文案要能让模型知道"你给的是什么"——否则它只能瞎猜
    assert "wrong_field" in text


@pytest.mark.asyncio
async def test_unknown_tool_name_becomes_visible_error() -> None:
    """模型调了一个不存在的工具 → 错误结果，不抛异常。"""
    loop, _, _ = _make_loop(
        [_tool_call_round('{"message": "x"}', name="nonexistent"), _text_round("换")],
        tools=[EchoTool()],
    )

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    from sigma_agent.agent_messages import ToolResultAgentMessage

    tool_results = [m for m in result.messages if isinstance(m, ToolResultAgentMessage)]
    assert tool_results[0].is_error is True
    assert "未注册的工具" in tool_results[0].content[0].text  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_rounds_returns_stopped_not_error() -> None:
    """轮数耗尽 → ``stopped``，不是异常、也不是 ``completed``。"""
    # 三轮都要工具，但 max_rounds=2
    loop, _, _ = _make_loop(
        [
            _tool_call_round('{"message": "1"}'),
            _tool_call_round('{"message": "2"}'),
            _tool_call_round('{"message": "3"}'),
        ],
        tools=[EchoTool()],
        max_rounds=2,
    )

    result = await loop.run_turn(_history())

    assert result.status == "stopped"
    assert result.rounds == 2
    assert "max_rounds" in result.reason


@pytest.mark.asyncio
async def test_no_tool_call_completes_in_one_round() -> None:
    """模型直接给文本、不调工具 → 一轮结束。"""
    loop, _, _ = _make_loop([_text_round("直接回答")])

    result = await loop.run_turn(_history())

    assert result.status == "completed"
    assert result.rounds == 1
    assert result.text == "直接回答"
    assert len(result.messages) == 1


@pytest.mark.asyncio
async def test_determinism_two_runs_are_identical() -> None:
    """**两次执行产出逐字节一致**（架构 7.2 节的地基）。

    时间戳固定是这里的必要条件——真实时钟会让这条断言永远失败。
    """
    rounds = [_tool_call_round('{"message": "hi"}'), _text_round("完成")]

    async def run_once() -> str:
        tool = EchoTool()
        loop, _, _ = _make_loop(rounds, tools=[tool])
        result = await loop.run_turn(_history())
        return "\n".join(
            m.model_dump_json() if hasattr(m, "model_dump_json") else repr(m)
            for m in result.messages
        )

    assert await run_once() == await run_once()


@pytest.mark.asyncio
async def test_transcript_exhausted_raises_loudly() -> None:
    """transcript 用完后继续请求 → **抛错**，不返回空流。

    静默的空响应会让 loop 的测试出现"假通过"——看起来正常结束，
    实际根本没拿到数据（``fake.py`` 的 ``TranscriptExhausted`` docstring）。
    """
    loop, _, _ = _make_loop([_tool_call_round('{"message": "hi"}')], tools=[EchoTool()])

    with pytest.raises(TranscriptExhausted):
        await loop.run_turn(_history())


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------


def test_base_loop_has_exactly_one_subclass() -> None:
    """门槛 G30：「唯一 loop 契约」。

    留基类是为了将来能换循环策略，**但现在只允许一个子类**——
    否则"唯一的循环实现"这个判断会被静默架空，而架空的方式还是合规的
    （加个子类而已）。
    """
    assert BaseLoop.__subclasses__() == [AgentLoop]


def test_duplicate_tool_registration_keeps_registry_intact() -> None:
    """门槛 G24：重名抛错，**且注册表保持旧状态**。

    若实现成"先删后插"，失败时会留下**半更新状态**——
    症状是"某个工具永久消失"，不指向根因。
    """
    registry = ToolRegistry()
    first = EchoTool()
    registry.register(first)

    with pytest.raises(DuplicateToolError):
        registry.register(EchoTool())

    # 旧的还在，没被改坏
    assert registry.names() == ["echo"]
    assert registry.get("echo") is first


def test_tool_schema_is_derived_from_pydantic_params() -> None:
    """``json_schema()`` 由 ``params`` 自动生成——这是 Python 侧的现成优势。"""
    schema = EchoTool().json_schema()
    assert schema["properties"]["message"]["type"] == "string"
    assert "message" in schema["required"]


def test_registry_schema_order_is_stable() -> None:
    """``schemas()`` 顺序按字典序固定。

    常驻区哈希依赖它（门槛 G26）——顺序不稳定 = prompt cache 永远命中不了。
    """
    registry = ToolRegistry()
    registry.register(FailingTool())
    registry.register(EchoTool())
    registry.register(ExplodingTool())

    assert registry.names() == ["boom", "echo", "failing"]
    names_in_schema = [s["function"]["name"] for s in registry.schemas()]
    assert names_in_schema == ["boom", "echo", "failing"]
