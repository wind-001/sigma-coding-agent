"""清单延续（GoalContinuation，推进机制批次 3）的测试。

判据（GA-2 注入实验证伪，见 scripts/gate_injection_advance.py）：
    G-GC-1  无清单 → 不干预（91% 的任务形态，基线实测 2/22 建清单）；
    G-GC-2  清单有 pending/running → continue + inject 点名条数；
    G-GC-3  连续 max_continues 轮后放行（防"永远差一步"死循环）；
    G-GC-4  账本损坏/读不了 → 不干预（宽容是订阅者的策略——
             推进是增益，不是新的失败面；账本该坏由 todo 工具向模型报）；
    G-GC-5  loop 集成：continue 真多跑一轮，注入进 produced。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sigma.agent.types import ToolContext, ToolResult
from sigma.events.lifecycle import AdvanceFacts
from sigma.hooks.base import HookManager
from sigma.providers.base import NeverCancelled
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock
from sigma.providers.stamps import from_epoch as ts
from sigma.runtime.advance import GoalContinuation
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.base import BaseTool
from sigma.tools.builtin.todo import TODO_RELATIVE
from sigma.tools.registry import ToolRegistry
from pydantic import BaseModel

FIXED_TIME = ts(1_700_000_000)
_FACTS = AdvanceFacts(round_index=1, last_text="我做完了")


def _write_todo(workspace: Path, statuses: list[str]) -> None:
    """按给定状态列表造一本账（推进钩子只读 status，其余字段从简）。"""
    path = workspace / TODO_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "goal": "测试目标",
                "updated_at": "2026-10-13T00:00:00",
                "next_id": len(statuses) + 1,
                "items": [
                    {"id": i + 1, "title": f"任务{i + 1}", "status": s}
                    for i, s in enumerate(statuses)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 单元
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ledger_means_no_intervention(tmp_path: Path) -> None:
    """G-GC-1：没有清单 → 不干预（多数任务的形态）。"""
    gate = GoalContinuation(tmp_path)
    decision = await gate.advance(_FACTS)
    assert decision.continue_round is False
    assert decision.force_stop is False


@pytest.mark.asyncio
async def test_all_completed_means_no_intervention(tmp_path: Path) -> None:
    """清单全 completed → 不干预（目标达成的客观形态）。"""
    _write_todo(tmp_path, ["completed", "completed"])
    gate = GoalContinuation(tmp_path)
    decision = await gate.advance(_FACTS)
    assert decision.continue_round is False


@pytest.mark.asyncio
async def test_pending_items_continue_with_count(tmp_path: Path) -> None:
    """G-GC-2：有 pending/running → continue，inject 点名条数与出路。"""
    _write_todo(tmp_path, ["completed", "pending", "running"])
    gate = GoalContinuation(tmp_path)
    decision = await gate.advance(_FACTS)
    assert decision.continue_round is True
    assert decision.inject is not None
    assert "2 条未完成" in decision.inject
    assert 'todo(action="list")' in decision.inject
    assert "2 条未完成" in decision.reason


@pytest.mark.asyncio
async def test_max_continues_caps_the_loop(tmp_path: Path) -> None:
    """G-GC-3：连续推进有上限——超过就放行，不"永远差一步"。"""
    _write_todo(tmp_path, ["pending"])
    gate = GoalContinuation(tmp_path, max_continues=2)
    first = await gate.advance(_FACTS)
    second = await gate.advance(_FACTS)
    third = await gate.advance(_FACTS)
    assert first.continue_round is True
    assert second.continue_round is True
    assert third.continue_round is False  # 上限过了,放行收尾


@pytest.mark.asyncio
async def test_corrupt_ledger_is_ignored(tmp_path: Path) -> None:
    """G-GC-4：账本损坏 → 不干预（读不了 = 无从判断 = 放行）。

    推进钩子崩掉任务比放行糟糕得多——账本坏没坏该由 todo 工具变成
    模型可见的错误结果，不靠推进层崩掉树持久化。
    """
    path = tmp_path / TODO_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    gate = GoalContinuation(tmp_path)
    decision = await gate.advance(_FACTS)
    assert decision.continue_round is False


# ---------------------------------------------------------------------------
# loop 集成
# ---------------------------------------------------------------------------


class _NoopParams(BaseModel):
    pass


class _NoopTool(BaseTool):
    name = "noop"
    description = "空转工具"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _NoopParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text="ok")])


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_loop_continues_when_ledger_unfinished(tmp_path: Path) -> None:
    """G-GC-5：loop 侧——清单有 pending → 收手被续一轮，注入进 produced。

    第二轮的 transcript 之后没有更多输入（清单不会被这一轮改变），
    所以用 max_continues=1 的 gate：第一次 continue、第二次放行 →
    completed 且 rounds=2。
    """
    _write_todo(tmp_path, ["pending"])
    registry = ToolRegistry()
    registry.register(_NoopTool())
    hooks = HookManager()
    hooks.register_advance(GoalContinuation(tmp_path, max_continues=1))
    loop = AgentLoop(
        provider=FakeProvider.from_rounds(
            [_text_round("我做完了"), _text_round("好，先把清单清掉")]
        ),
        registry=registry,
        model="fake",
        workspace_root=tmp_path,
        signal=NeverCancelled(),
        clock=lambda: FIXED_TIME,
        hooks=hooks,
    )
    result = await loop.run_turn([])

    assert result.status == "completed"
    assert result.rounds == 2
    produced = " ".join(
        str(getattr(getattr(m, "message", None), "content", "")) for m in result.messages
    )
    assert "清单延续" in produced
    assert "1 条未完成" in produced
