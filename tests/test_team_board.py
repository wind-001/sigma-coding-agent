"""team_board 工具(团队任务板 + 双向信箱)与 sigma/team 状态机的门禁测试。

P4-团队任务详规 §4,七道门槛各有一条"注入变红"的靶子:
    G-TEAM-1 迁移表逐行参数化        → test_g_team1_transition_table_row 等;
    G-TEAM-2 claim 并发互斥          → test_concurrent_claims_exactly_one_wins;
    G-TEAM-3 inbox drain-once        → test_inbox_drain_once;
    G-TEAM-4 派发融合(开/关)         → test_sub_agent_gets_team_board / test_team_disabled_*;
    G-TEAM-5 板纪律                  → test_non_assignee_finish_fail_rejected 等;
    G-TEAM-6 预算回填与 G885         → test_g_team6_*;
    G-TEAM-7 原子写 + 四状态名       → test_board_write_uses_tmp_and_replace 等。

回归钉子:
    - enable_team_tasks=False 时常驻区与现状逐字节一致
      (test_resident_area_byte_identical_when_team_disabled);
    - 时钟必须可注入(test_injected_clock_does_not_advance);
    - .sigma/team/ 随 .sigma/ 排除在影子 checkpoint 之外(test_sigma_team_outside_checkpoint)。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest

from sigma.agent.types import ToolContext, ToolResult, TurnResult
from sigma.config.resident_caps import CAPS, MEASURED, RESIDENT_BUDGET_TOKENS, caps_sum
from sigma.providers.base import NeverCancelled
from sigma.providers.fake import FakeProvider
from sigma.providers.tokens import estimate_text
from sigma.sdk import InteractiveSession, build_system_prompt, default_registry
from sigma.security.shadow_checkpoint import BUILTIN_EXCLUDES
from sigma.tools.registry import ToolRegistry
from sigma.runtime.sub_agent import TaskParams, TaskTool

from sigma.team.board import (
    ABSENT,
    STATES,
    TRANSITIONS,
    Board,
    TeamTask,
    apply,
)
from sigma.tools.builtin.team import BOARD_RELATIVE, TeamBoard, TeamBoardParams

FIXED_TIME = "2026-09-29T12:00:00.000"
LEAD = "lead-1"


# ---------------------------------------------------------------------------
# 公共基建
# ---------------------------------------------------------------------------


def _clock() -> str:
    return FIXED_TIME


def _tool(lead: str = LEAD) -> TeamBoard:
    return TeamBoard(lead_session_id=lead, clock=_clock)


def _ctx(tmp_path: Path, session_id: str = "sess-1") -> ToolContext:
    return ToolContext(
        session_id=session_id, workspace_root=tmp_path, signal=NeverCancelled()
    )


async def _board_op(tool: TeamBoard, ctx: ToolContext, **kwargs: Any) -> ToolResult:
    return await tool.run(TeamBoardParams(action="board", **kwargs), ctx)


async def _create(
    tool: TeamBoard, ctx: ToolContext, title: str = "调查 X", **kwargs: Any
) -> ToolResult:
    return await _board_op(tool, ctx, op="create", title=title, **kwargs)


def _board_file(tmp_path: Path) -> Path:
    return tmp_path / BOARD_RELATIVE


def _load_board(tmp_path: Path) -> dict[str, Any]:
    raw = json.loads(_board_file(tmp_path).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _text(result: ToolResult) -> str:
    assert result.content, "结果必须有 content"
    block = result.content[0]
    return getattr(block, "text", "")


# ---------------------------------------------------------------------------
# G-TEAM-1:迁移表逐行钉住(详规 §2.1,直接喂纯状态机,不碰盘)
# ---------------------------------------------------------------------------


def _board_in(pre: str, task_id: str = "t1") -> Board:
    """造一个处于指定前置状态的板。pre ∈ absent / pending / running / success / fail。"""
    board = Board()
    if pre == ABSENT:
        return board
    task = TeamTask(id=task_id, title="任务", deps=[])
    task.state = pre
    if pre in ("running", "success", "fail"):
        task.assignee = "w1"
    if pre == "fail":
        task.result = "旧原因"
    board.tasks.append(task)
    board.next_id = 2
    return board


def _check_create(task: TeamTask) -> None:
    assert task.id == "t1", "create 分配 id"
    assert task.assignee == "" and task.deps == []


def _check_claim(task: TeamTask) -> None:
    assert task.assignee == "w1", "claim 记 assignee"


def _check_finish(task: TeamTask) -> None:
    assert task.result == "结论", "finish 写 result"


def _check_fail(task: TeamTask) -> None:
    assert task.result == "原因", "fail 写 reason(落 result 字段)"


def _check_reclaim_running(task: TeamTask) -> None:
    assert task.assignee == "", "running+reclaim 清 assignee"
    assert task.note == "接管", "留 note(强制接管的审计痕迹)"


def _check_reclaim_fail(task: TeamTask) -> None:
    assert task.assignee == "" and task.result == "", "fail+reclaim 清 result/assignee"
    assert task.note == "接管"


@pytest.mark.parametrize(
    ("pre", "event", "kwargs", "expected", "verify"),
    [
        (ABSENT, "create", {"title": "新任务"}, "pending", _check_create),
        ("pending", "claim", {"task_id": "t1", "caller": "w1"}, "running", _check_claim),
        ("running", "finish", {"task_id": "t1", "caller": "w1", "result": "结论"}, "success", _check_finish),
        ("running", "fail", {"task_id": "t1", "caller": "w1", "result": "原因"}, "fail", _check_fail),
        ("running", "reclaim", {"task_id": "t1", "caller": LEAD, "lead": LEAD, "note": "接管"}, "pending", _check_reclaim_running),
        ("fail", "reclaim", {"task_id": "t1", "caller": LEAD, "lead": LEAD, "note": "接管"}, "pending", _check_reclaim_fail),
    ],
    ids=[
        "absent+create",
        "pending+claim",
        "running+finish",
        "running+fail",
        "running+reclaim",
        "fail+reclaim",
    ],
)
def test_g_team1_transition_table_row(
    pre: str, event: str, kwargs: dict[str, Any], expected: str,
    verify: Callable[[TeamTask], None],
) -> None:
    """G-TEAM-1 的靶子:§2.1 迁移表**每一行**一条用例——合法迁移生效。

    注入变红:删掉 TRANSITIONS 里对应的一行,或改次态/动作,本行当场红。
    """
    board = _board_in(pre)
    task = apply(board, event, **kwargs)
    assert task.state == expected, f"{pre} + {event} 应到 {expected}"
    verify(task)


_EVENTS = ("create", "claim", "finish", "fail", "reclaim")
_LEGAL_KEYS = set(TRANSITIONS)
_ILLEGAL_ROWS = [
    (pre, event)
    for pre in ("pending", "running", "success", "fail")
    for event in _EVENTS
    if (pre, event) not in _LEGAL_KEYS
]


@pytest.mark.parametrize(("pre", "event"), _ILLEGAL_ROWS, ids=[f"{p}+{e}" for p, e in _ILLEGAL_ROWS])
def test_g_team1_illegal_transition_raises(pre: str, event: str) -> None:
    """表里没有 (当前状态, 事件) 的组合一律 ValueError,消息带状态/事件/调用者。

    注入变红:给 success 添出边、或把抛错改成静默返回,这里当场红。
    """
    board = _board_in(pre)
    with pytest.raises(ValueError) as excinfo:
        apply(
            board,
            event,
            task_id="t1",
            caller="w1" if pre != "pending" else "w2",
            lead=LEAD,
            title="x",
            result="x",
            note="x",
        )
    message = str(excinfo.value)
    assert pre in message and event in message, f"报错要携带状态与事件:{message}"


def test_g_team1_claim_guard_requires_deps_success() -> None:
    """claim 的守卫:deps 未全部 success 拒绝(逐个非 success 状态都试一遍)。"""
    for dep_state in ("pending", "running", "fail"):
        board = Board()
        dep = TeamTask(id="t0", title="前置", deps=[])
        dep.state = dep_state
        task = TeamTask(id="t1", title="后续", deps=["t0"])
        board.tasks = [dep, task]
        board.next_id = 2
        with pytest.raises(ValueError, match="t0"):
            apply(board, "claim", task_id="t1", caller="w1")


def test_g_team1_claim_allowed_when_deps_success() -> None:
    """对照:依赖全 success 后 claim 放行。"""
    board = _board_in("success")
    task = TeamTask(id="t2", title="后续", deps=["t1"])
    board.tasks.append(task)
    claimed = apply(board, "claim", task_id="t2", caller="w9")
    assert claimed.state == "running" and claimed.assignee == "w9"


def test_g_team1_create_guard_requires_deps_success() -> None:
    """create 的守卫:deps 未全 success(含不存在的 id)直接拒绝——
    板上出现的任务都是"可开工"的;不存在 id 的依赖永远无法被 claim。"""
    board = _board_in("pending")
    with pytest.raises(ValueError, match="t1"):
        apply(board, "create", title="后续", deps=["t1"])
    with pytest.raises(ValueError, match="t99"):
        apply(Board(), "create", title="悬空", deps=["t99"])


def test_g_team1_reclaim_requires_lead() -> None:
    """reclaim 仅 lead 可调:worker 与空 lead 都不行。"""
    for caller, lead in (("w1", LEAD), ("w1", "")):
        board = _board_in("running")
        with pytest.raises(ValueError, match="lead"):
            apply(board, "reclaim", task_id="t1", caller=caller, lead=lead, note="x")


def test_g_team1_finish_fail_guard_caller_is_assignee() -> None:
    """finish/fail 的守卫:调用者 != assignee 拒绝。"""
    for event in ("finish", "fail"):
        board = _board_in("running")
        with pytest.raises(ValueError, match="w2"):
            apply(board, event, task_id="t1", caller="w2", result="x")


# ---------------------------------------------------------------------------
# G-TEAM-7(状态名部分):恰为四值
# ---------------------------------------------------------------------------


def test_g_team7_state_names_exactly_four() -> None:
    """状态名恰为 pending/running/success/fail 四值,不多不少。"""
    assert set(STATES) == {"pending", "running", "success", "fail"}
    assert len(STATES) == 4
    # from_dict 对未知状态宁可崩不要错
    with pytest.raises(ValueError, match="状态"):
        TeamTask.from_dict({"id": "t1", "title": "x", "state": "claimed"})


# ---------------------------------------------------------------------------
# G-TEAM-2:claim 并发互斥
# ---------------------------------------------------------------------------


async def test_concurrent_claims_exactly_one_wins(tmp_path: Path) -> None:
    """G-TEAM-2 的靶子:两个并发 claim 同一任务,恰一个成功。

    主会话与子 agent 经 registry clone **共享同一个 TeamBoard 实例**,
    因此共享同一把 store 实例锁——锁不共享,互斥就是名义上的。
    """
    tool = _tool()
    await _create(tool, _ctx(tmp_path))
    results = await asyncio.gather(
        tool.run(TeamBoardParams(action="board", op="claim", id="t1"),
                 _ctx(tmp_path, "agent-a")),
        tool.run(TeamBoardParams(action="board", op="claim", id="t1"),
                 _ctx(tmp_path, "agent-b")),
    )
    winners = [r for r in results if not r.is_error]
    assert len(winners) == 1, f"恰一个成功,实际 {len(winners)}"
    data = _load_board(tmp_path)
    assert data["tasks"][0]["assignee"] in ("agent-a", "agent-b")
    assert data["tasks"][0]["state"] == "running"


# ---------------------------------------------------------------------------
# G-TEAM-3:inbox drain-once
# ---------------------------------------------------------------------------


async def test_inbox_drain_once(tmp_path: Path) -> None:
    """G-TEAM-3 的靶子:同一条消息第二次 inbox 不再出现。"""
    tool = _tool()
    sender = _ctx(tmp_path, "agent-a")
    reader = _ctx(tmp_path, LEAD)
    sent = await tool.run(
        TeamBoardParams(action="send", to="lead", text="t1 做完了,结论 A"), sender
    )
    assert sent.is_error is False
    first = await tool.run(TeamBoardParams(action="inbox"), reader)
    assert first.is_error is False
    assert "结论 A" in _text(first) and "agent-a" in _text(first)
    second = await tool.run(TeamBoardParams(action="inbox"), reader)
    assert second.is_error is False
    assert "结论 A" not in _text(second), "drain-once:读过的消息不得重复出现"


async def test_inbox_empty_is_info_not_error(tmp_path: Path) -> None:
    result = await _tool().run(TeamBoardParams(action="inbox"), _ctx(tmp_path))
    assert result.is_error is False


async def test_send_requires_to_and_text(tmp_path: Path) -> None:
    tool = _tool()
    missing_to = await tool.run(
        TeamBoardParams(action="send", text="x"), _ctx(tmp_path)
    )
    assert missing_to.is_error is True
    missing_text = await tool.run(
        TeamBoardParams(action="send", to="lead", text="  "), _ctx(tmp_path)
    )
    assert missing_text.is_error is True


async def test_send_to_lead_writes_lead_inbox(tmp_path: Path) -> None:
    """to=lead 解析到主会话 id:子 agent 不必知道 lead 的真实 session-id。"""
    tool = _tool(lead=LEAD)
    await tool.run(
        TeamBoardParams(action="send", to="lead", text="进展"), _ctx(tmp_path, "agent-a")
    )
    lead_inbox = tmp_path / ".sigma" / "team" / "inbox" / f"{LEAD}.jsonl"
    assert lead_inbox.exists()
    line = json.loads(lead_inbox.read_text(encoding="utf-8").splitlines()[0])
    assert line["sender"] == "agent-a"
    assert line["at"] == FIXED_TIME


async def test_send_to_session_id_writes_that_inbox(tmp_path: Path) -> None:
    tool = _tool()
    await tool.run(
        TeamBoardParams(action="send", to="agent-b", text="请认领 t2"),
        _ctx(tmp_path, "agent-a"),
    )
    inbox_b = tmp_path / ".sigma" / "team" / "inbox" / "agent-b.jsonl"
    assert inbox_b.exists()
    drained = await tool.run(TeamBoardParams(action="inbox"), _ctx(tmp_path, "agent-b"))
    assert "请认领 t2" in _text(drained)


async def test_inbox_corrupt_file_errors_and_preserves(tmp_path: Path) -> None:
    """半截 JSON:报错且**不删文件**——drain-once 只在完整读出后才清,
    静默清掉读不出的信箱等于吞消息。"""
    tool = _tool()
    inbox = tmp_path / ".sigma" / "team" / "inbox"
    inbox.mkdir(parents=True)
    target = inbox / "sess-1.jsonl"
    target.write_text('{"sender": "agent-a", ', encoding="utf-8")
    result = await tool.run(TeamBoardParams(action="inbox"), _ctx(tmp_path))
    assert result.is_error is True
    assert target.exists()


# ---------------------------------------------------------------------------
# 工具层:板操作与板纪律(G-TEAM-5 的工具面)
# ---------------------------------------------------------------------------


async def test_create_assigns_sequential_ids_and_renders_board(tmp_path: Path) -> None:
    tool = _tool()
    result = await _create(tool, _ctx(tmp_path))
    assert result.is_error is False
    assert "t1" in _text(result) and "调查 X" in _text(result)
    assert "[pending]" in _text(result)
    second = await _create(tool, _ctx(tmp_path), "修复 Y")
    assert "t2" in _text(second)
    data = _load_board(tmp_path)
    assert [t["id"] for t in data["tasks"]] == ["t1", "t2"]
    assert data["next_id"] == 3, "next_id 单调递增,id 永不复用"


async def test_create_requires_title(tmp_path: Path) -> None:
    result = await _create(_tool(), _ctx(tmp_path), "   ")
    assert result.is_error is True


async def test_finish_on_pending_task_rejected(tmp_path: Path) -> None:
    """create 后直接 finish 是非法迁移(迁移表没有 pending+finish 行)。"""
    tool = _tool()
    await _create(tool, _ctx(tmp_path))
    result = await _board_op(tool, _ctx(tmp_path), op="finish", id="t1", result="结论")
    assert result.is_error is True
    assert "claim" in _text(result), "报错要告诉模型正确的路径:先 claim"


async def test_finish_requires_result(tmp_path: Path) -> None:
    tool = _tool()
    ctx = _ctx(tmp_path)
    await _create(tool, ctx)
    await _board_op(tool, ctx, op="claim", id="t1")
    result = await _board_op(tool, ctx, op="finish", id="t1", result="  ")
    assert result.is_error is True


async def test_finish_and_fail_record_outcome(tmp_path: Path) -> None:
    tool = _tool()
    ctx = _ctx(tmp_path)
    await _create(tool, ctx)
    await _board_op(tool, ctx, op="claim", id="t1")
    result = await _board_op(tool, ctx, op="finish", id="t1", result="结论 A")
    assert result.is_error is False
    assert "[success]" in _text(result) and "结论 A" in _text(result)
    await _create(tool, ctx, "第二条")
    await _board_op(tool, ctx, op="claim", id="t2")
    result = await _board_op(tool, ctx, op="fail", id="t2", result="走不通:缺依赖")
    assert result.is_error is False
    assert "[fail" in _text(result), "fail 是 4 字符状态名,渲染带填充也应可辨"
    data = _load_board(tmp_path)
    assert data["tasks"][0]["state"] == "success"
    assert data["tasks"][1]["state"] == "fail"


async def test_finish_on_finished_task_rejected(tmp_path: Path) -> None:
    """success 是吸收态,无出边:重复收尾报错。"""
    tool = _tool()
    ctx = _ctx(tmp_path)
    await _create(tool, ctx)
    await _board_op(tool, ctx, op="claim", id="t1")
    await _board_op(tool, ctx, op="finish", id="t1", result="结论 A")
    for op in ("finish", "fail", "claim"):
        result = await _board_op(tool, ctx, op=op, id="t1", result="x")
        assert result.is_error is True


async def test_reclaim_returns_task_to_pending(tmp_path: Path) -> None:
    """lead 强制接管:running/fail → pending,清 assignee(/result),留 note。"""
    tool = _tool()
    worker = _ctx(tmp_path, "agent-a")
    lead = _ctx(tmp_path, LEAD)
    await _create(tool, lead)
    await _board_op(tool, worker, op="claim", id="t1")
    result = await _board_op(
        tool, lead, op="reclaim", id="t1", note="子任务超时,重派"
    )
    assert result.is_error is False
    assert "[pending]" in _text(result)
    data = _load_board(tmp_path)
    assert data["tasks"][0]["state"] == "pending"
    assert data["tasks"][0]["assignee"] == ""
    assert data["tasks"][0]["note"] == "子任务超时,重派"
    # 重派后可被再次认领
    again = await _board_op(tool, worker, op="claim", id="t1")
    assert again.is_error is False


async def test_reclaim_on_failed_task_clears_result(tmp_path: Path) -> None:
    tool = _tool()
    worker = _ctx(tmp_path, "agent-a")
    lead = _ctx(tmp_path, LEAD)
    await _create(tool, lead)
    await _board_op(tool, worker, op="claim", id="t1")
    await _board_op(tool, worker, op="fail", id="t1", result="做不成")
    result = await _board_op(tool, lead, op="reclaim", id="t1", note="换个思路重试")
    assert result.is_error is False
    data = _load_board(tmp_path)
    assert data["tasks"][0]["state"] == "pending"
    assert data["tasks"][0]["result"] == ""
    assert data["tasks"][0]["note"] == "换个思路重试"


async def test_reclaim_requires_note(tmp_path: Path) -> None:
    tool = _tool()
    worker = _ctx(tmp_path, "agent-a")
    lead = _ctx(tmp_path, LEAD)
    await _create(tool, lead)
    await _board_op(tool, worker, op="claim", id="t1")
    result = await _board_op(tool, lead, op="reclaim", id="t1", note="  ")
    assert result.is_error is True, "无审计痕迹的强制接管不允许"


async def test_non_assignee_finish_fail_rejected(tmp_path: Path) -> None:
    """G-TEAM-5 的靶子:非 assignee 的 finish/fail 被拒——**含主 agent**。"""
    tool = _tool()
    worker = _ctx(tmp_path, "agent-a")
    lead = _ctx(tmp_path, LEAD)
    await _create(tool, lead)
    await _board_op(tool, worker, op="claim", id="t1")
    for op in ("finish", "fail"):
        result = await _board_op(tool, lead, op=op, id="t1", result="我来收尾")
        assert result.is_error is True, f"lead {op} 他人 running 任务必须被拒"
    ok = await _board_op(tool, worker, op="finish", id="t1", result="正常收尾")
    assert ok.is_error is False


async def test_non_assignee_cannot_claim_running_task(tmp_path: Path) -> None:
    tool = _tool()
    await _create(tool, _ctx(tmp_path, LEAD))
    await _board_op(tool, _ctx(tmp_path, "agent-a"), op="claim", id="t1")
    result = await _board_op(tool, _ctx(tmp_path, "agent-b"), op="claim", id="t1")
    assert result.is_error is True


def _seed_board(tmp_path: Path, tasks: list[TeamTask], *, next_id: int | None = None) -> Path:
    """直接落一块板:create 守卫的正常路径到不了"依赖未完成"的板,
    但 reclaim 回退/人工修复可能留下它——守卫必须在工具面也拦住它。"""
    board = Board(updated_at=FIXED_TIME, next_id=next_id or len(tasks) + 1, tasks=tasks)
    board_file = tmp_path / ".sigma" / "team" / "board.json"
    board_file.parent.mkdir(parents=True, exist_ok=True)
    board_file.write_text(
        json.dumps(board.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    return board_file


async def test_claim_blocked_until_deps_success(tmp_path: Path) -> None:
    """G-TEAM-5 的靶子:deps 未全 success 时 claim 被拒;全 success 后放行。"""
    tool = _tool()
    ctx = _ctx(tmp_path, "agent-a")
    _seed_board(
        tmp_path,
        [
            TeamTask(id="t1", title="前置任务", state="pending"),
            TeamTask(id="t2", title="后续任务", deps=["t1"]),
        ],
    )
    blocked = await _board_op(tool, ctx, op="claim", id="t2")
    assert blocked.is_error is True
    assert "t1" in _text(blocked), "拒绝理由要点名未完成的依赖"
    # 依赖 success 后放行
    await _board_op(tool, ctx, op="claim", id="t1")
    await _board_op(tool, ctx, op="finish", id="t1", result="前置完成")
    allowed = await _board_op(tool, ctx, op="claim", id="t2")
    assert allowed.is_error is False


async def test_claim_blocked_by_failed_dep(tmp_path: Path) -> None:
    """fail 的依赖不算 success:下游任务不能开工(纯状态机侧另有参数化覆盖)。"""
    tool = _tool()
    ctx = _ctx(tmp_path, "agent-a")
    _seed_board(
        tmp_path,
        [
            TeamTask(id="t1", title="前置任务", state="fail", result="做不成"),
            TeamTask(id="t2", title="后续任务", deps=["t1"]),
        ],
    )
    result = await _board_op(tool, ctx, op="claim", id="t2")
    assert result.is_error is True


async def test_create_with_unknown_dep_rejected(tmp_path: Path) -> None:
    result = await _create(_tool(), _ctx(tmp_path), "悬空任务", deps=["t99"])
    assert result.is_error is True


async def test_claim_unknown_task_rejected(tmp_path: Path) -> None:
    result = await _board_op(_tool(), _ctx(tmp_path), op="claim", id="t99")
    assert result.is_error is True


async def test_claim_requires_id(tmp_path: Path) -> None:
    result = await _board_op(_tool(), _ctx(tmp_path), op="claim")
    assert result.is_error is True


async def test_list_empty_board_is_info_not_error(tmp_path: Path) -> None:
    result = await _board_op(_tool(), _ctx(tmp_path), op="list")
    assert result.is_error is False


# ---------------------------------------------------------------------------
# G-TEAM-7:原子写(tmp + os.replace)
# ---------------------------------------------------------------------------


async def test_board_write_uses_tmp_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-TEAM-7 的靶子:board.json 经 tmp+rename 落盘,不留 .tmp 残骸。"""
    import sigma.team.store as store_module

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", spy)
    result = await _create(_tool(), _ctx(tmp_path))
    assert result.is_error is False
    assert len(calls) == 1
    src, dst = calls[0]
    assert src.endswith("board.json.tmp")
    assert dst.endswith("board.json")
    assert not _board_file(tmp_path).with_name("board.json.tmp").exists()


async def test_atomic_write_survives_concurrent_ops(tmp_path: Path) -> None:
    """G-TEAM-7 的行为面:进程内并发写板,文件始终是完整合法的 JSON,
    id 不重不漏,无交错损坏。"""
    tool = _tool()
    ctxs = [_ctx(tmp_path, f"agent-{i}") for i in range(6)]
    created = await asyncio.gather(
        *[_create(tool, ctx, f"任务 {i}") for i, ctx in enumerate(ctxs)]
    )
    assert all(not r.is_error for r in created)
    claimed = await asyncio.gather(
        *[
            _board_op(tool, ctx, op="claim", id=f"t{i + 1}")
            for i, ctx in enumerate(ctxs)
        ]
    )
    assert all(not r.is_error for r in claimed)
    data = _load_board(tmp_path)
    ids = [t["id"] for t in data["tasks"]]
    assert ids == [f"t{i + 1}" for i in range(6)], "并发 create 的 id 必须唯一且有序"
    for index, task in enumerate(data["tasks"]):
        assert task["state"] == "running"
        assert task["assignee"] == f"agent-{index}"
    assert [t["state"] for t in data["tasks"]] and set(
        t["state"] for t in data["tasks"]
    ) <= set(STATES)
    leftovers = list(_board_file(tmp_path).parent.glob("*.tmp"))
    assert leftovers == [], "tmp 文件必须被 rename 消费,不得残留"


async def test_failed_guard_leaves_board_untouched(tmp_path: Path) -> None:
    """守卫拒绝 = 板上分毫未动:事务在异常时不落盘。"""
    tool = _tool()
    ctx = _ctx(tmp_path)
    await _create(tool, ctx, "只有一条")
    before = _load_board(tmp_path)
    result = await _board_op(tool, ctx, op="claim", id="t99")
    assert result.is_error is True
    assert _load_board(tmp_path) == before


# ---------------------------------------------------------------------------
# 时钟注入
# ---------------------------------------------------------------------------


async def test_injected_clock_does_not_advance(tmp_path: Path) -> None:
    """注入 clock 后,两次相隔真实睡眠的写入时间戳完全相同——
    板与信箱的时间源必须可注入,测试才能离线且确定。"""
    tool = _tool()
    ctx = _ctx(tmp_path)
    await _create(tool, ctx)
    await asyncio.sleep(0.02)
    await tool.run(
        TeamBoardParams(action="send", to="sess-1", text="稍后的消息"),
        _ctx(tmp_path, "agent-a"),
    )
    assert _load_board(tmp_path)["updated_at"] == FIXED_TIME
    # to=sess-1:信箱按**收件人**分文件,发件人是 agent-a
    inbox = tmp_path / ".sigma" / "team" / "inbox" / "sess-1.jsonl"
    line = json.loads(inbox.read_text(encoding="utf-8").splitlines()[0])
    assert line["at"] == FIXED_TIME
    assert line["sender"] == "agent-a"


# ---------------------------------------------------------------------------
# G-TEAM-4:派发融合
# ---------------------------------------------------------------------------


class _RecordingFakeProvider(FakeProvider):
    """记录每次 stream 收到的工具名集合(与 test_sub_agent.py 同款取证)。"""

    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        super().__init__(rounds)
        self.seen_tool_names: list[list[str]] = []

    def stream(self, messages: Any, tools: Any, **kwargs: Any) -> Any:
        self.seen_tool_names.append(
            sorted(entry["function"]["name"] for entry in tools)
        )
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
    enable_team_tasks: bool,
) -> tuple[InteractiveSession, _RecordingFakeProvider]:
    provider = _RecordingFakeProvider(rounds)
    session = InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        registry=ToolRegistry(),
        system_prompt=build_system_prompt(task=True),
        enable_compaction=False,
        enable_checkpoint=False,
        enable_sub_agent=True,
        enable_team_tasks=enable_team_tasks,
        session_id="sess-main",
    )
    return session, provider


async def test_main_session_registers_team_board(tmp_path: Path) -> None:
    session, _ = _session(tmp_path, [_text_round("ok")], enable_team_tasks=True)
    assert "team_board" in session._registry.names()


async def test_sub_agent_gets_team_board(tmp_path: Path) -> None:
    """G-TEAM-4 的靶子:enable_team_tasks 的子 agent 注册了 TeamBoard。

    并顺带取证**实例共享**:子 agent 经共享实例发 to=lead,lead 用主会话
    身份能 drain 到——同一块板、同一套信箱,不另起第二份。
    """
    rounds = [
        _call_round("c1", "task", action="dispatch", description="板任务 t1:调查 X"),
        _call_round(
            "s1", "team_board", action="send", to="lead", text="板任务 t1 完成"
        ),
        _text_round("子任务完成"),
        _text_round("已派发"),
        _text_round("收到"),
    ]
    session, provider = _session(tmp_path, rounds, enable_team_tasks=True)
    await session.send("主任务")
    sub_tools = provider.seen_tool_names[1]
    assert "team_board" in sub_tools, f"子 agent 应看到 team_board:{sub_tools}"
    assert "task" not in sub_tools
    # lead 侧 drain:子 agent 的 to=lead 落进了主会话的信箱
    board_tool = session._registry.get("team_board")
    drained = await board_tool.run(
        TeamBoardParams(action="inbox"),
        ToolContext(
            session_id="sess-main", workspace_root=tmp_path, signal=NeverCancelled()
        ),
    )
    assert drained.is_error is False
    assert "板任务 t1 完成" in _text(drained)
    assert "sess-main-sa1" in _text(drained), "发件人应是子会话 id"


async def test_team_disabled_no_registration(tmp_path: Path) -> None:
    """关闭时不注册:主会话与子 agent 的工具集里都没有 team_board。"""
    rounds = [
        _call_round("c1", "task", action="dispatch", description="普通子任务"),
        _text_round("子任务完成"),
        _text_round("已派发"),
        _text_round("收到"),
    ]
    session, provider = _session(tmp_path, rounds, enable_team_tasks=False)
    assert "team_board" not in session._registry.names()
    await session.send("主任务")
    assert "team_board" not in provider.seen_tool_names[1]


def test_cli_no_team_flag_wiring() -> None:
    """--no-team 单独关 team(默认随 --sub-agent);--no-sub-agent 连带关。

    两处组装(一次性 / SessionManager)共用 cli._team_tasks_enabled——
    这里钉住旗标到开关的换算,组装点本身由 CLI 集成路径覆盖。
    """
    from sigma.cli.main import _team_tasks_enabled, build_parser

    parser = build_parser()
    defaults = parser.parse_args([])
    assert defaults.sub_agent is True and defaults.no_team is False
    assert _team_tasks_enabled(defaults) is True
    assert _team_tasks_enabled(parser.parse_args(["--no-team"])) is False
    assert _team_tasks_enabled(parser.parse_args(["--no-sub-agent"])) is False
    # 显式双开照常
    assert _team_tasks_enabled(parser.parse_args(["--sub-agent"])) is True


async def test_duplicate_team_board_registration_rejected(tmp_path: Path) -> None:
    """registry 里已有 team_board 再开 enable_team_tasks:报错,不静默覆盖。"""
    first, _ = _session(tmp_path, [_text_round("ok")], enable_team_tasks=True)
    with pytest.raises(ValueError, match="team_board"):
        InteractiveSession(
            provider=FakeProvider([_text_round("ok")]),
            workspace_root=tmp_path,
            model="fake",
            registry=first._registry,
            enable_team_tasks=True,
            session_id="sess-other",
        )


async def test_dispatch_receipt_carries_board_discipline(tmp_path: Path) -> None:
    """派发 description 纪律(详规 §2.3)经 dispatch 回执可见:开 team 才出现。"""
    async def factory(
        description: str, ctx: ToolContext, sub_id: str, max_rounds: int
    ) -> TurnResult:
        return TurnResult(status="completed", messages=[], text="done", rounds=1)

    on = TaskTool(factory=factory, team_hint=True)
    off = TaskTool(factory=factory)
    result_on = await on.run(
        TaskParams(action="dispatch", description="x"), _ctx(tmp_path)
    )
    result_off = await off.run(
        TaskParams(action="dispatch", description="x"), _ctx(tmp_path)
    )
    assert "team_board" in _text(result_on)
    assert "team_board" not in _text(result_off)
    await on.wait_and_drain()
    await off.wait_and_drain()


# ---------------------------------------------------------------------------
# G-TEAM-6:预算(实测数字进 MEASURED,G885 不破)
# ---------------------------------------------------------------------------


def _optional_registry_with_team() -> ToolRegistry:
    """与 resident_caps"可选栏"同口径的注册表:联网 2 + task + team_board。"""
    from sigma.sdk import web_fetch_tool, web_search_tool

    async def factory(
        description: str, ctx: ToolContext, sub_id: str, max_rounds: int
    ) -> TurnResult:
        return TurnResult(status="completed", messages=[], text="", rounds=0)

    registry = ToolRegistry()
    registry.register(web_search_tool(api_key="k"))
    registry.register(web_fetch_tool(api_key="k"))
    registry.register(TaskTool(factory=factory))
    registry.register(TeamBoard(lead_session_id="sess-main"))
    return registry


def test_g_team6_measured_backfilled_and_within_cap() -> None:
    """TeamBoard 的 schema 实测数字必须回填 MEASURED,且可选栏不超 cap 1250。"""
    schemas = _optional_registry_with_team().schemas()
    total = estimate_text(
        json.dumps(schemas, ensure_ascii=False, sort_keys=True)
    )
    key = "工具 schema(可选:联网 2+task+team_board)"
    assert key in MEASURED, "实测数字必须进 resident_caps.MEASURED"
    assert MEASURED[key] == total, "MEASURED 必须与实测一致(表即常量,漂移当场红)"
    cap_key = "工具 schema(可选:联网+task+team_board)"
    assert cap_key in CAPS
    assert total <= CAPS[cap_key], (
        f"可选栏实测 {total} 超过 cap {CAPS[cap_key]}——按消费纪律停下上报,"
        "不得自行改总额"
    )


def test_g885_caps_sum_still_equals_budget() -> None:
    """分项和 == 5500:team_board 落既有分项,总额分毫不动。"""
    assert caps_sum() == RESIDENT_BUDGET_TOKENS == 5500


# ---------------------------------------------------------------------------
# 回归钉子:team 关时常驻区逐字节一致;.sigma/team/ 不进影子 checkpoint
# ---------------------------------------------------------------------------


def test_resident_area_byte_identical_when_team_disabled() -> None:
    """enable_team_tasks=False 时,常驻区与既有路径逐字节一致。

    取证方式:参照组 = 与既有路径同款的注册表(sub_agent 开、team 关);
    实验组 = 参照组 + team_board。**移除 team_board 后两者必须逐字节一致**——
    这条同时拦住"顺手改了 task/todo 的 description"一类漂移。
    提示词侧:build_system_prompt 的产物与 team 开关无关(prompts/ 零变化)。
    """
    async def factory(
        description: str, ctx: ToolContext, sub_id: str, max_rounds: int
    ) -> TurnResult:
        return TurnResult(status="completed", messages=[], text="", rounds=0)

    reference = default_registry(todo=True)
    reference.register(TaskTool(factory=factory))
    team_on = reference.clone()
    team_on.register(TeamBoard(lead_session_id="sess-main"))

    reference_json = json.dumps(
        reference.schemas(), ensure_ascii=False, sort_keys=True
    )
    on_minus_team = json.dumps(
        [
            definition
            for definition in team_on.schemas()
            if definition["function"]["name"] != "team_board"
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert reference_json == on_minus_team
    # 提示词零变化:没有 team 工具行,也没有任何 team 字样的注入点
    prompt = build_system_prompt(task=True)
    assert "team_board" not in prompt
    assert "team_board" in json.dumps(team_on.schemas(), ensure_ascii=False)


def test_sigma_team_outside_checkpoint_scope() -> None:
    """G-TEAM-6 旁注:.sigma/team/ 不进影子 checkpoint——.sigma/ 已整体排除。"""
    assert ".sigma/" in BUILTIN_EXCLUDES


# ---------------------------------------------------------------------------
# 收尾:工具元数据
# ---------------------------------------------------------------------------


def test_team_board_is_a_write_tool() -> None:
    """team_board 落盘(.sigma/team/),不是 read_only——写批次持 tool_lock。"""
    tool = _tool()
    assert tool.read_only is False
    assert tool.name == "team_board"
    assert "board" in tool.description and "inbox" in tool.description
