"""``todo`` 工具（P4 任务清单）的门禁测试。

需求与设计见 ``docs/plans/P4-任务清单工具-详规.md``：
三状态 pending/running/completed、单向流转（completed→pending 允许返工）、
同一时刻至多一条 running、create 防误覆盖、断点重续（文件在 .sigma/ 下，
checkpoint 回滚碰不到）。

**这里的测试是 G82 注入的靶子**：把 ``_update`` 里"至多一条 running"
的检查分支删掉，``test_update_rejects_second_running`` 必须变红。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.base import NeverCancelled
from sigma_tools.todo import TODO_RELATIVE, TodoParams, TodoTool


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="test",
        workspace_root=tmp_path,
        signal=NeverCancelled(),
    )


async def _run(tool: TodoTool, ctx: ToolContext, **kwargs: object) -> ToolResult:
    params = TodoParams.model_validate(kwargs)
    return await tool.run(params, ctx)


def _load_raw(tmp_path: Path) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((tmp_path / TODO_RELATIVE).read_text(encoding="utf-8")),
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_writes_file_and_renders(tmp_path: Path) -> None:
    tool, ctx = TodoTool(), _ctx(tmp_path)
    result = await _run(
        tool, ctx,
        action="create", goal="修好解析器",
        items=["读测试", "改代码"],
    )
    assert not result.is_error
    text = result.content[0].text  # type: ignore[index]
    assert "已创建" in text and "修好解析器" in text and "pending" in text
    raw = _load_raw(tmp_path)
    assert raw["goal"] == "修好解析器"
    items = cast(list[dict[str, object]], raw["items"])
    assert [it["status"] for it in items] == ["pending", "pending"]
    assert raw["updated_at"]  # 本地毫秒时间戳（项目约定），存在即可


async def test_create_requires_goal_and_items(tmp_path: Path) -> None:
    tool, ctx = TodoTool(), _ctx(tmp_path)
    missing_goal = await _run(tool, ctx, action="create", items=["a"])
    assert missing_goal.is_error
    missing_items = await _run(tool, ctx, action="create", goal="g")
    assert missing_items.is_error
    assert not (tmp_path / TODO_RELATIVE).exists()


async def test_create_refuses_overwrite_without_flag(tmp_path: Path) -> None:
    """防误覆盖：已有清单时 create 必须报错，返回里带当前清单。"""
    tool, ctx = TodoTool(), _ctx(tmp_path)
    await _run(tool, ctx, action="create", goal="旧目标", items=["旧任务"])
    refused = await _run(
        tool, ctx, action="create", goal="新目标", items=["新任务"]
    )
    assert refused.is_error
    assert "防误覆盖" in refused.content[0].text  # type: ignore[index]
    # 拒绝后原清单原封不动
    raw = _load_raw(tmp_path)
    assert raw["goal"] == "旧目标"


async def test_create_overwrite_flag_rebuilds(tmp_path: Path) -> None:
    tool, ctx = TodoTool(), _ctx(tmp_path)
    await _run(tool, ctx, action="create", goal="旧目标", items=["旧任务"])
    rebuilt = await _run(
        tool, ctx, action="create", goal="新目标",
        items=["a", "b"], overwrite=True,
    )
    assert not rebuilt.is_error
    assert rebuilt.details["overwritten"] is True
    raw = _load_raw(tmp_path)
    assert raw["goal"] == "新目标"


# ---------------------------------------------------------------------------
# update：状态机
# ---------------------------------------------------------------------------


async def test_full_lifecycle_forward(tmp_path: Path) -> None:
    tool, ctx = TodoTool(), _ctx(tmp_path)
    await _run(tool, ctx, action="create", goal="g", items=["a", "b"])
    step1 = await _run(tool, ctx, action="update", id=1, status="running")
    assert not step1.is_error
    step2 = await _run(tool, ctx, action="update", id=1, status="completed")
    assert not step2.is_error
    assert "1/2 completed" in step2.content[0].text  # type: ignore[index]
    raw = _load_raw(tmp_path)
    items = cast(list[dict[str, object]], raw["items"])
    assert [it["status"] for it in items] == ["completed", "pending"]


async def test_update_rejects_second_running(tmp_path: Path) -> None:
    """「依次执行」的机器化：同一时刻至多一条 running。G82 注入的靶子。"""
    tool, ctx = TodoTool(), _ctx(tmp_path)
    await _run(
        tool, ctx, action="create", goal="g",
        items=["a", "b"],
    )
    first = await _run(tool, ctx, action="update", id=1, status="running")
    assert not first.is_error
    second = await _run(tool, ctx, action="update", id=2, status="running")
    assert second.is_error, "第二条 running 必须被拒绝"
    assert "只允许一条 running" in second.content[0].text  # type: ignore[index]
    raw = _load_raw(tmp_path)
    items = cast(list[dict[str, object]], raw["items"])
    assert [it["status"] for it in items] == ["running", "pending"], "被拒的那条不得被改动"


async def test_update_idempotent_same_status(tmp_path: Path) -> None:
    """同状态幂等允许——断点重续时模型会重复 update，这是正常操作。"""
    tool, ctx = TodoTool(), _ctx(tmp_path)
    await _run(tool, ctx, action="create", goal="g", items=["a"])
    await _run(tool, ctx, action="update", id=1, status="running")
    again = await _run(tool, ctx, action="update", id=1, status="running")
    assert not again.is_error
    assert "保持 running" in again.content[0].text  # type: ignore[index]


async def test_reopen_completed_to_pending(tmp_path: Path) -> None:
    """completed → pending 是返工重开，显式允许。"""
    tool, ctx = TodoTool(), _ctx(tmp_path)
    await _run(tool, ctx, action="create", goal="g", items=["a"])
    await _run(tool, ctx, action="update", id=1, status="running")
    await _run(tool, ctx, action="update", id=1, status="completed")
    reopen = await _run(tool, ctx, action="update", id=1, status="pending")
    assert not reopen.is_error
    raw = _load_raw(tmp_path)
    items = cast(list[dict[str, object]], raw["items"])
    assert items[0]["status"] == "pending"


async def test_update_rejects_skip_and_backward(tmp_path: Path) -> None:
    tool, ctx = TodoTool(), _ctx(tmp_path)
    await _run(tool, ctx, action="create", goal="g", items=["a", "b"])
    skip = await _run(tool, ctx, action="update", id=1, status="completed")
    assert skip.is_error, "pending → completed 跳级必须拒绝"
    await _run(tool, ctx, action="update", id=1, status="running")
    backward = await _run(tool, ctx, action="update", id=1, status="pending")
    assert backward.is_error, "running → pending 回退必须拒绝"


async def test_update_detail_only(tmp_path: Path) -> None:
    tool, ctx = TodoTool(), _ctx(tmp_path)
    await _run(tool, ctx, action="create", goal="g", items=["a"])
    result = await _run(tool, ctx, action="update", id=1, detail="验收：测试全绿")
    assert not result.is_error
    raw = _load_raw(tmp_path)
    items = cast(list[dict[str, object]], raw["items"])
    assert items[0]["detail"] == "验收：测试全绿"
    assert items[0]["status"] == "pending"


async def test_update_bad_id_and_missing_file(tmp_path: Path) -> None:
    tool, ctx = TodoTool(), _ctx(tmp_path)
    no_file = await _run(tool, ctx, action="update", id=1, status="running")
    assert no_file.is_error
    await _run(tool, ctx, action="create", goal="g", items=["a"])
    bad = await _run(tool, ctx, action="update", id=99, status="running")
    assert bad.is_error
    assert "没有 id=99" in bad.content[0].text  # type: ignore[index]


# ---------------------------------------------------------------------------
# list 与损坏文件
# ---------------------------------------------------------------------------


async def test_list_without_file_says_so(tmp_path: Path) -> None:
    tool, ctx = TodoTool(), _ctx(tmp_path)
    result = await _run(tool, ctx, action="list")
    assert not result.is_error  # 没有清单是正常状态，不是错误
    assert "还没有任务清单" in result.content[0].text  # type: ignore[index]


async def test_corrupt_file_is_error_not_silent_reset(tmp_path: Path) -> None:
    """半截 JSON 必须报错。静默当成"没有清单"会让模型 create 覆盖掉
    还有三条没做完的计划——宁可崩不要错。"""
    tool, ctx = TodoTool(), _ctx(tmp_path)
    path = tmp_path / TODO_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text('{"goal": "截断', encoding="utf-8")
    result = await _run(tool, ctx, action="list")
    assert result.is_error
    assert "无法解析" in result.content[0].text  # type: ignore[index]


# ---------------------------------------------------------------------------
# revise：计划随执行进化（星辰 2026-09-24）+ 难度系数
# ---------------------------------------------------------------------------


async def _three_step_plan(ctx: ToolContext, tool: TodoTool) -> None:
    await _run(
        tool, ctx, action="create", goal="修好渲染器",
        items=["定位 bug", "修 inline", "修 renderer"],
    )


async def test_revise_replaces_only_pending(tmp_path: Path) -> None:
    """**G90 的靶子**：revise 只换 pending，已完成与正在跑的一条都不动。

    注入（允许 revise 动 running/completed）后：本用例红——
    清单里的 #1（completed）与 #2（running）会被换掉/消失。
    """
    ctx = _ctx(tmp_path)
    tool = TodoTool()
    await _three_step_plan(ctx, tool)
    # 单向流转：pending → running → completed（直接跳 completed 会被状态机拒绝）
    await _run(tool, ctx, action="update", id=1, status="running")
    await _run(tool, ctx, action="update", id=1, status="completed")
    await _run(tool, ctx, action="update", id=2, status="running")

    result = await _run(
        tool, ctx, action="revise",
        items=["拆成两步修 inline", "修 renderer", "补回归测试"],
    )
    assert not result.is_error
    raw = _load_raw(tmp_path)
    items = cast(list[dict[str, object]], raw["items"])
    done = [it for it in items if it["status"] == "completed"]
    running = [it for it in items if it["status"] == "running"]
    assert len(done) == 1 and done[0]["id"] == 1
    assert len(running) == 1 and running[0]["id"] == 2, "正在跑的不能被换掉"
    assert sum(1 for it in items if it["status"] == "pending") == 3


async def test_revise_reports_dropped_items(tmp_path: Path) -> None:
    """丢弃必须可见：被换掉的旧 pending 要逐条报出来，不能悄悄消失。"""
    ctx = _ctx(tmp_path)
    tool = TodoTool()
    await _three_step_plan(ctx, tool)
    result = await _run(tool, ctx, action="revise", items=["重新拆的一步"])
    text = cast(str, result.content[0].text)  # type: ignore[union-attr]
    assert "替换 3 条" in text
    assert "#3 修 renderer" in text, "被换掉的条目必须点名"


async def test_revise_keeps_one_running_invariant(tmp_path: Path) -> None:
    """revise 之后"至多一条 running"仍然成立（新条目全是 pending）。"""
    ctx = _ctx(tmp_path)
    tool = TodoTool()
    await _three_step_plan(ctx, tool)
    await _run(tool, ctx, action="update", id=1, status="running")
    await _run(tool, ctx, action="revise", items=["后续 A", "后续 B"])
    result = await _run(tool, ctx, action="update", id=3, status="running")
    assert result.is_error, "已有 #1 在 running，#3 不能也变 running"


async def test_revise_without_pending_is_error(tmp_path: Path) -> None:
    """没有可调整的后续任务 → 报错，不是静默清空清单。"""
    ctx = _ctx(tmp_path)
    tool = TodoTool()
    await _run(tool, ctx, action="create", goal="单步任务", items=["只有一步"])
    await _run(tool, ctx, action="update", id=1, status="running")
    result = await _run(tool, ctx, action="revise", items=["新的后续"])
    assert result.is_error
    assert "没有可调整的后续任务" in cast(str, result.content[0].text)  # type: ignore[union-attr]


async def test_revise_new_ids_never_reuse_old(tmp_path: Path) -> None:
    """被替换掉的 id 不再复用——否则"新计划"与"被放弃的旧计划"在历史里分不清。"""
    ctx = _ctx(tmp_path)
    tool = TodoTool()
    await _three_step_plan(ctx, tool)
    await _run(tool, ctx, action="revise", items=["新后续"])
    raw = _load_raw(tmp_path)
    items = cast(list[dict[str, object]], raw["items"])
    assert [it["id"] for it in items] == [4]


async def test_difficulty_is_recorded_and_rendered(tmp_path: Path) -> None:
    """难度随条目落盘并在清单里可见（与子 agent 三档同词汇）。"""
    ctx = _ctx(tmp_path)
    tool = TodoTool()
    await _run(
        tool, ctx, action="create", goal="修好队列",
        items=["读代码", "修四处缺陷"], difficulty=["low", "high"],
    )
    result = await _run(tool, ctx, action="list")
    text = cast(str, result.content[0].text)  # type: ignore[union-attr]
    assert "【低】" in text and "【高】" in text


async def test_difficulty_length_mismatch_rejected(tmp_path: Path) -> None:
    """平行数组对不上 → 直接拒绝。猜着配对会让难度落到错误的条目上。"""
    ctx = _ctx(tmp_path)
    tool = TodoTool()
    result = await _run(
        tool, ctx, action="create", goal="x",
        items=["a", "b", "c"], difficulty=["low"],
    )
    assert result.is_error
    assert "一一对应" in cast(str, result.content[0].text)  # type: ignore[union-attr]


async def test_update_does_not_touch_difficulty(tmp_path: Path) -> None:
    """难度只有一个写入口（create/revise），update 不插手——
    与"title 不给 update"同一条纪律：两个入口改同一字段，约定会漏。"""
    ctx = _ctx(tmp_path)
    tool = TodoTool()
    await _run(
        tool, ctx, action="create", goal="y", items=["a", "b"],
        difficulty=["low", "high"],
    )
    await _run(tool, ctx, action="update", id=1, status="running")
    raw = _load_raw(tmp_path)
    items = cast(list[dict[str, object]], raw["items"])
    assert items[0]["difficulty"] == "low"
