"""P5-批次1 的门槛测试：观测层采集端（TraceHook）。

每条测试对应详规里的一张门槛卡（G865–G868/G871/G872），
**每条都有"注入变红"的路径**（scripts/gate_injection_batch16.py）——
断言写的是时机、数值与不变量，实现一旦退化（漏发事件 / 双重计时 /
异常穿透 / 观测污染会话）就当场红。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from sigma.tools.base import BaseTool
from sigma.events.lifecycle import (
    ApprovalDecision,
    TurnEnd,
)
from sigma.hooks.base import (
    ApprovalHook,
    HookManager,
)
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.registry import ToolRegistry
from sigma.agent.types import ToolContext, ToolResult, TurnResult
from sigma.providers.base import CancelToken
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock
from sigma.sessions.context import SessionContext
from sigma.hooks.persist import SessionPersistHook
from sigma.sessions.store import JsonlStore
from sigma.observability.trace import TraceHook, trace_path_for
from sigma.sessions.tree import SessionTree

from sigma.providers.stamps import from_epoch as ts

FIXED_TIME = ts(1_700_000_000)


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class _FakeTimer:
    """确定性计时器：每次调用 +0.5s。t 序列由事件序唯一决定。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.5
        return self.t


# ---------------------------------------------------------------------------
# 测试用工具与转录（与 test_hooks_persistence 同款）
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
        return ToolResult(
            content=[TextBlock(text=f"echo: {args}")], details={"truncated": False}
        )


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


def _make_loop(
    rounds: list[list[dict[str, Any]]],
    *,
    hooks: HookManager | None,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(EchoTool())
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


def _make_trace(
    sessions_dir: Path, warnings: list[str] | None = None
) -> TraceHook:
    """固定 now/timer 的 TraceHook；warn 注入收集器（默认丢弃）。"""
    return TraceHook(
        "s1",
        sessions_dir,
        now=lambda: FIXED_TIME,
        timer=_FakeTimer(),
        warn=warnings.append if warnings is not None else (lambda _msg: None),
    )


def _read_lines(sessions_dir: Path, session_id: str = "s1") -> list[dict[str, Any]]:
    path = trace_path_for(sessions_dir, session_id)
    assert path.is_file(), f"trace 文件不存在：{path}"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ---------------------------------------------------------------------------
# G865：事件逐行落盘，kind 序列 == 事件序
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g865_trace_lines_follow_event_order(tmp_path: Path) -> None:
    """一 turn 回放后，trace 文件逐事件成行、kind 序列与事件序一致。

    事件序（loop 的发射点决定）：LlmRequested → AssistantProduced(llm_end) →
    ToolStart → ToolEnd →（第 2 轮）LlmRequested → llm_end → TurnEnd。
    """
    hooks = HookManager()
    hooks.register(_make_trace(tmp_path))
    loop = _make_loop(
        [_tool_call_round('{"message": "a"}', call_id="call_1"), _text_round("done")],
        hooks=hooks,
    )
    await loop.run_turn([])

    lines = _read_lines(tmp_path)
    assert [entry["kind"] for entry in lines] == [
        "llm_requested",
        "llm_end",
        "tool_start",
        "tool_end",
        "llm_requested",
        "llm_end",
        "turn_end",
    ]
    assert lines[1]["round"] == 1
    assert lines[4]["round"] == 2
    assert lines[3]["name"] == "echo"
    assert lines[3]["ok"] is True
    assert lines[3]["tool_call_id"] == "call_1"


# ---------------------------------------------------------------------------
# G866：latency/ttft 精确等于注入 timer 的推进量
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g866_latency_equals_injected_timer_deltas(tmp_path: Path) -> None:
    """不真跑钟：latency_ms / ttft_ms / duration_ms 全部等于 fake timer 差值。

    _FakeTimer 每次 +0.5s，且每个事件恰好取一次 t：
    轮 1（工具轮）llm_req=0.5 → llm_end=1.0 → latency 500ms；
    tool_start=1.5 → tool_end=2.0 → duration 500ms；
    轮 2（文本轮）llm_req=2.5 → 首个 TextChunk=3.0（ttft 500ms）→
    llm_end=3.5 → latency 1000ms。
    """
    hooks = HookManager()
    hooks.register(_make_trace(tmp_path))
    loop = _make_loop(
        [_tool_call_round('{"message": "a"}', call_id="call_1"), _text_round("done")],
        hooks=hooks,
    )
    await loop.run_turn([])

    lines = _read_lines(tmp_path)
    llm1 = lines[1]
    assert llm1["latency_ms"] == 500
    assert "ttft_ms" not in llm1  # 工具轮没有文本，TTFT 缺席而不是 0
    assert lines[3]["duration_ms"] == 500
    llm2 = lines[5]
    assert llm2["latency_ms"] == 1000
    assert llm2["ttft_ms"] == 500


# ---------------------------------------------------------------------------
# G867：trace 写入失败 → turn 照常完成、恰一条警告、后续零尝试
#       （与 G92 持久化"宁可崩"刻意相反——分界是审计凭据 vs 派生视图）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g867_write_failure_is_self_tolerant(tmp_path: Path) -> None:
    """落盘失败不崩任务：警告恰一次，自禁用后事件零尝试。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("占位文件——让 trace 路径的 parent 变成文件，mkdir 必炸")
    warnings: list[str] = []
    hook = TraceHook(
        "s1",
        blocker / "sub",
        now=lambda: FIXED_TIME,
        timer=_FakeTimer(),
        warn=warnings.append,
    )
    hooks = HookManager()
    hooks.register(hook)
    loop = _make_loop([_text_round("done")], hooks=hooks)
    result = await loop.run_turn([])  # 不抛——与 G92 形成刻意对照

    assert result.status == "completed"
    assert len(warnings) == 1
    assert "trace" in warnings[0]
    # 墓碑：自禁用后再喂事件，零尝试、零新警告
    hook.on_event(
        TurnEnd(status="completed", rounds=1, prompt_tokens=0, completion_tokens=0)
    )
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# G868：审批批准与拒绝都留痕，拒绝行带 reason
# ---------------------------------------------------------------------------


class _FixedGate(ApprovalHook):
    """恒定决定：allowed 与 reason 由构造给定。"""

    def __init__(self, decision: ApprovalDecision) -> None:
        self.name = "fixed-gate"
        self._decision = decision

    async def approve(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ApprovalDecision:
        return self._decision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision, allowed",
    [
        (ApprovalDecision(allowed=False, reason="命中危险模式:rm -rf ~"), False),
        (ApprovalDecision(allowed=True), True),
    ],
)
async def test_g868_approval_decisions_are_recorded(
    tmp_path: Path, decision: ApprovalDecision, allowed: bool
) -> None:
    hooks = HookManager()
    hooks.register(_make_trace(tmp_path))
    hooks.register_approval(_FixedGate(decision))
    # 拒绝也以工具结果回到模型 → loop 再请求一轮；转录要有收尾文本轮
    loop = _make_loop(
        [_tool_call_round('{"message": "a"}'), _text_round("ok")], hooks=hooks
    )
    await loop.run_turn([])

    lines = _read_lines(tmp_path)
    approvals = [e for e in lines if e["kind"] == "approval"]
    assert len(approvals) == 1
    entry = approvals[0]
    assert entry["allowed"] is allowed
    assert entry["name"] == "echo"
    if not allowed:
        assert "rm -rf" in entry["reason"]
    # 事件序：审批结论落在 ToolStart 之后、ToolEnd 之前（询问点在两者之间）
    kinds = [e["kind"] for e in lines]
    assert kinds.index("approval") > kinds.index("tool_start")
    assert kinds.index("approval") < kinds.index("tool_end")


# ---------------------------------------------------------------------------
# G871：观测零污染——trace 不进 produced / 不进会话树
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g871_observation_pollutes_nothing(tmp_path: Path) -> None:
    """同转录跑两遍：有 trace 与无 trace 的 produced、**落盘重载后的树**同形。

    树走 store 并在跑完后**重新从磁盘加载**再比对——"观测写进会话文件"
    这类污染在内存里看不出来，落盘重载才现形（clean=False / 节点数变多）。
    """
    rounds = [
        _tool_call_round('{"message": "a"}', call_id="call_1"),
        _text_round("done"),
    ]

    def build(hooks: HookManager | None, root: Path) -> tuple[AgentLoop, SessionContext]:
        context = SessionContext(
            system_prompt="测试",
            tools_schema=[],
            clock=lambda: FIXED_TIME,
            tree=SessionTree(store=JsonlStore(root, "s1")),
        )
        registry = ToolRegistry()
        registry.register(EchoTool())
        loop = AgentLoop(
            provider=FakeProvider.from_rounds(rounds),
            registry=registry,
            model="fake",
            workspace_root=".",
            max_rounds=20,
            signal=_NeverCancelled(),
            clock=lambda: FIXED_TIME,
            hooks=hooks,
        )
        return loop, context

    # 两边都落盘、都挂持久化；唯一差异 = 有没有 trace。
    bare_root = tmp_path / "bare"
    bare_root.mkdir()
    bare_loop, bare_ctx = build(HookManager(), bare_root)
    bare_loop._hooks.register(SessionPersistHook(bare_ctx))  # noqa: SLF001
    bare_result: TurnResult = await bare_loop.run_turn([])

    hooked_root = tmp_path / "hooked"
    hooked_root.mkdir()
    hooks = HookManager()
    hooked_loop, hooked_ctx = build(hooks, hooked_root)
    hooks.register(SessionPersistHook(hooked_ctx))
    hooks.register(_make_trace(hooked_root))
    hooked_result: TurnResult = await hooked_loop.run_turn([])

    assert [type(m) for m in bare_result.messages] == [
        type(m) for m in hooked_result.messages
    ]
    # 落盘事实重载比对：观测零污染 = 文件里只有持久化写的合法记录。
    # 坏行（trace 的观测行若被写进来就是这个形状）走 skipped_lines，
    # 不进 _corrupt——所以"零跳行"才是污染的正确断言点。
    bare_load = JsonlStore(bare_root, "s1").load()
    hooked_load = JsonlStore(hooked_root, "s1").load()
    assert bare_load.skipped_lines == []
    assert hooked_load.skipped_lines == []
    bare_reloaded = SessionTree.from_store(JsonlStore(bare_root, "s1"))
    hooked_reloaded = SessionTree.from_store(JsonlStore(hooked_root, "s1"))
    assert bare_reloaded.clean and hooked_reloaded.clean
    assert len(bare_reloaded) == len(hooked_reloaded)
    assert [type(m) for m in bare_reloaded.history()] == [
        type(m) for m in hooked_reloaded.history()
    ]
    # trace 侧确实写了行——排除"两边都没数据"的假绿
    assert _read_lines(hooked_root)


# ---------------------------------------------------------------------------
# G872：确定性——同回放跑两遍，trace 逐字节相同
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g872_trace_is_deterministic(tmp_path: Path) -> None:
    """固定 now/timer 下，两遍回放的 trace 文件逐字节一致（真实时钟引用=红）。"""
    rounds = [
        _tool_call_round('{"message": "a"}', call_id="call_1"),
        _text_round("done"),
    ]
    digests: list[str] = []
    for i in (1, 2):
        hooks = HookManager()
        hooks.register(_make_trace(tmp_path / f"run{i}"))
        loop = _make_loop(rounds, hooks=hooks)
        await loop.run_turn([])
        digests.append(
            trace_path_for(tmp_path / f"run{i}", "s1").read_text(encoding="utf-8")
        )
    assert digests[0] == digests[1]


# ---------------------------------------------------------------------------
# trace_path_for：净化与 JsonlStore 同源
# ---------------------------------------------------------------------------


def test_trace_path_shares_store_sanitization(tmp_path: Path) -> None:
    store = JsonlStore(tmp_path, "a/b")
    trace = trace_path_for(tmp_path, "a/b")
    assert trace.name == "a_b.trace.jsonl"
    assert trace.parent == store.path.parent
    assert trace.stem.startswith(store.path.stem)
