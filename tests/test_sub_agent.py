"""InteractiveSession 的 sub_agent 接线测试 + G84 注入靶子。

端到端用例走的是**真工厂**（``InteractiveSession._make_sub_agent_factory``）：
主会话 dispatch → 子会话被真实构造出来 → 子会话与主会话共用同一个 FakeProvider
（游标共享）。轮次消费顺序（也是 seen_tool_names 的下标顺序）——**子任务在主 r2
之前抢跑**（dispatch 的 gather 挂起点让事件循环先调度后台任务，FIFO）：

    0 主 r1 dispatch 调用
    1 子 r1 子会话第一轮（todo create）
    2 子 r2 子会话收尾
    3 主 r2 文本（收尾 → 兜底等子任务、注入回报）
    4 主 r3 兜底注入后的最终收尾
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from sigma.sdk import InteractiveSession, build_system_prompt
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import ToolContext
from sigma_ai.base import NeverCancelled
from sigma_ai.fake import FakeProvider

from sigma_tools.task import SubAgentRounds, TaskParams
from sigma_tools.todo import TodoTool


# ---------------------------------------------------------------------------
# 记录型 provider 与轮次构造
# ---------------------------------------------------------------------------


class _RecordingFakeProvider(FakeProvider):
    """记录每次 stream 收到的工具名集合与消息文本——

    "子会话 registry 无 task"（G84）与"上下文隔离"都靠它做端到端取证：
    断言的是**子会话实际发出去的请求**，不是内部结构。
    """

    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        super().__init__(rounds)
        self.seen_tool_names: list[list[str]] = []
        self.seen_message_texts: list[str] = []

    def stream(self, messages: Any, tools: Any, **kwargs: Any) -> Any:
        self.seen_tool_names.append(
            sorted(entry["function"]["name"] for entry in tools)
        )
        texts: list[str] = []
        for m in messages:
            content = getattr(m, "content", None)
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        texts.append(text)
        self.seen_message_texts.append("\n".join(texts))
        return super().stream(messages, tools, **kwargs)


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _call_round(
    call_id: str, name: str, **arguments: Any
) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": call_id,
            "name": name,
            "arguments_delta": json.dumps(arguments, ensure_ascii=False),
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _session(
    tmp_path: Path,
    rounds: list[list[dict[str, Any]]],
    *,
    enable_sub_agent: bool = True,
    registry: ToolRegistry | None = None,
) -> tuple[InteractiveSession, _RecordingFakeProvider]:
    provider = _RecordingFakeProvider(rounds)
    reg = registry if registry is not None else ToolRegistry()
    session = InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        registry=reg,
        system_prompt=build_system_prompt(task=enable_sub_agent),
        enable_compaction=False,
        enable_checkpoint=False,
        enable_sub_agent=enable_sub_agent,
        session_id="sess-main",
    )
    return session, provider


def _user_texts(messages: list[Any]) -> list[str]:
    found: list[str] = []
    for m in messages:
        inner = getattr(m, "message", None)
        content = getattr(inner, "content", None)
        if isinstance(content, str):
            found.append(content)
    return found


# ---------------------------------------------------------------------------
# 接线
# ---------------------------------------------------------------------------


async def test_session_with_sub_agent_registers_task(tmp_path: Path) -> None:
    session, _ = _session(tmp_path, [_text_round("ok")], enable_sub_agent=True)
    assert "task" in session._registry.names()


async def test_session_without_sub_agent_has_no_task(tmp_path: Path) -> None:
    """默认路径（不传 enable_sub_agent）与加它之前逐字节一致。"""
    session, _ = _session(tmp_path, [_text_round("ok")], enable_sub_agent=False)
    assert "task" not in session._registry.names()


async def test_duplicate_task_registration_rejected(tmp_path: Path) -> None:
    """registry 里已有 task 再开 enable_sub_agent：报错，不静默覆盖。"""
    first, _ = _session(tmp_path, [_text_round("ok")], enable_sub_agent=True)
    with pytest.raises(ValueError, match="task"):
        _session(
            tmp_path,
            [_text_round("ok")],
            enable_sub_agent=True,
            registry=first._registry,
        )


def test_task_line_follows_flag() -> None:
    """提示词与注册表同源：task 行只在 task=True 时出现。"""
    assert "- task：派子任务" in build_system_prompt(task=True)
    assert "- task：派子任务" not in build_system_prompt()


# ---------------------------------------------------------------------------
# 端到端：dispatch → 子会话 → 信箱回报
# ---------------------------------------------------------------------------


async def test_dispatch_runs_sub_session_and_reports_back(tmp_path: Path) -> None:
    rounds = [
        _call_round("c1", "task", action="dispatch", description="去调查 X"),
        # ↓ 子会话（先于主 r2 抢跑，消费 1、2）
        _call_round(
            "s1", "todo", action="create", goal="子目标", items=["子步骤 1"]
        ),
        _text_round("子任务总结：查到了，结论 C。"),
        # ↑
        _text_round("已派发，等结果"),
        _text_round("收到回报，主任务完成。"),
    ]
    registry = ToolRegistry()
    registry.register(TodoTool())
    session, provider = _session(
        tmp_path, rounds, enable_sub_agent=True, registry=registry
    )
    result = await session.send("主任务：调查 X")

    # 1) 主上下文收到了回报（两条注入路径之一）：
    #    FakeProvider 无真挂起点 → 子任务在主 r1 批次的 gather 期间一口气跑完，
    #    回报在主 r2 顶部被 drain 注入（rounds=2）。子任务慢时走收尾兜底
    #    （rounds=3），由 test_task_tool.py 的 G86 用例覆盖。
    reports = [t for t in _user_texts(result.messages) if "子任务回报" in t]
    assert len(reports) == 1 and "结论 C" in reports[0]
    assert result.rounds == 2

    # 2) G84 的靶子：子会话实际请求里的工具集**没有 task**
    sub_tools = provider.seen_tool_names[1]
    assert "task" not in sub_tools, f"子会话不该看到 task：{sub_tools}"
    assert "todo" in sub_tools

    # 3) todo 账本独立：子账本存在，主账本没被碰
    sub_todo = tmp_path / ".sigma" / "todo-sess-main-sa1.json"
    assert sub_todo.exists(), "子 agent 的清单应落在自己的账本里"
    assert not (tmp_path / ".sigma" / "todo.json").exists(), "主账本不应被子 agent 触碰"

    # 4) 上下文隔离：子会话的请求里没有主会话的任务文本
    assert "主任务：调查 X" not in provider.seen_message_texts[1]


# ---------------------------------------------------------------------------
# 轮数预算三档：端到端（预算真的被子会话执行）
# ---------------------------------------------------------------------------


async def test_sub_session_round_budget_is_enforced(tmp_path: Path) -> None:
    """给 1 轮预算，子会话就只能跑 1 轮——**行为级**断言，不看私有属性。

    为什么要有这条（而不是只测 TaskTool 传了什么）
        "派发方传了 max_rounds"与"子会话真的按它跑"是两件事：
        中间隔着工厂闭包与 InteractiveSession 的构造。少接一次线，
        三档预算就变成名义开关——名字叫 low，实际还是跑满 50 轮。
    """
    # 一个"永远要工具"的 round：不设预算它会一直跑下去
    always_call = _call_round("c1", "todo", action="list")
    session, _ = _session(
        tmp_path, [always_call] * 8, enable_sub_agent=True
    )
    factory = session._make_sub_agent_factory(asyncio.Lock(), SubAgentRounds())
    ctx = ToolContext(
        session_id="sess-main", workspace_root=tmp_path, signal=NeverCancelled()
    )
    result = await factory("做一件很长的活", ctx, "sess-main-sa1", max_rounds=1)
    assert result.status == "stopped", "1 轮预算跑不完 → 必须以 stopped 收场"
    assert result.rounds == 1


async def test_high_budget_allows_more_rounds(tmp_path: Path) -> None:
    """对照：预算给 3 轮就能跑 3 轮——证明上面那条不是"永远 stopped"。"""
    always_call = _call_round("c1", "todo", action="list")
    session, _ = _session(
        tmp_path, [always_call] * 8, enable_sub_agent=True
    )
    factory = session._make_sub_agent_factory(asyncio.Lock(), SubAgentRounds())
    ctx = ToolContext(
        session_id="sess-main", workspace_root=tmp_path, signal=NeverCancelled()
    )
    result = await factory("做一件很长的活", ctx, "sess-main-sa1", max_rounds=3)
    assert result.rounds == 3


async def test_dispatch_passes_level_budget_to_sub_session(tmp_path: Path) -> None:
    """low 档（10 轮）经真工厂传到子会话：子会话的轮数上限 = 10。

    取证方式：给一个"永远要工具"的 provider，子会话跑满预算后回来，
    rounds 必须等于该档位的预算值。
    """
    always_call = _call_round("c1", "todo", action="list")
    session, _ = _session(
        tmp_path, [always_call] * 20, enable_sub_agent=True
    )
    tool = session._registry.get("task")
    ctx = ToolContext(
        session_id="sess-main", workspace_root=tmp_path, signal=NeverCancelled()
    )
    await tool.run(
        TaskParams(action="dispatch", description="长活", difficulty="low"), ctx
    )
    messages = await tool.wait_and_drain()  # type: ignore[attr-defined]
    assert len(messages) == 1
    # 回报里带轮数：low 档预算 10 → 子会话最多跑满 10 轮
    body = messages[0].message.content  # type: ignore[union-attr]
    assert "（10 轮" in body, f"low 档应给 10 轮预算，实际回报：{body[:80]}"
