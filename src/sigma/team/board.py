"""团队任务板状态机:迁移表即代码,纯逻辑、零 I/O、零 sigma 内部依赖(纯 stdlib)。

P4-团队任务详规 §2.1(星辰拍板 2026-09-30):状态 pending / running / success / fail;
迁移表(当前状态 × 事件 → 次态)就是下面那张字面量 dict——**表即规范**,
G-TEAM-1 逐行参数化钉住:改表不补测试(或反之)当场红。

为什么独立成包且只用 stdlib
    协作域(状态机 / 存储 / 信箱)是规则密集而框架知识为零的代码:它不需要
    知道 ToolContext、pydantic 或任何 provider——给它一个 Board 和一个事件,
    它给答案。依赖越少,这张表越接近"可以直接读的规范"。
    (代价:时间戳助手必须在包内自足,见 :func:`now_stamp`。)

非法迁移一律 ``ValueError``(携带 当前状态/事件/调用者)——**宁可崩不要错**:
    静默吞掉非法迁移,"板上到底是什么状态"就只能靠约定保证,而约定会漏。
    调用方(tools/builtin/team.py 薄壳)负责把 ValueError 转成 is_error 的
    ToolResult(架构 4.2:工具失败返回 is_error,不向上抛)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Sequence, cast

#: 合法状态,恰四值(G-TEAM-7)。顺序 = 生命周期序(渲染与文档同序)。
STATES: Final[tuple[str, ...]] = ("pending", "running", "success", "fail")

#: create 事件的"当前状态":板上还没有这个 id。用中文原文,报错可直读详规的表。
ABSENT: Final[str] = "(不存在)"

#: 事件名。与工具 board 子操作**同名**——薄壳按名字转发,不设第二套词汇。
CREATE: Final[str] = "create"
CLAIM: Final[str] = "claim"
FINISH: Final[str] = "finish"
FAIL: Final[str] = "fail"
RECLAIM: Final[str] = "reclaim"

#: 迁移表(§2.1):(当前状态, 事件) → 次态。**这张 dict 就是规范本身**。
#: success 是吸收态,无出边;守卫与动作在 :func:`apply` 里按事件分支。
TRANSITIONS: Final[dict[tuple[str, str], str]] = {
    (ABSENT, CREATE): "pending",
    ("pending", CLAIM): "running",
    ("running", FINISH): "success",
    ("running", FAIL): "fail",
    ("running", RECLAIM): "pending",
    ("fail", RECLAIM): "pending",
}


def now_stamp() -> str:
    """包内默认时间源:本地时间、毫秒,格式与 ``sigma.providers.stamps.now`` 一致。

    为什么不 import stamps:``sigma.team`` 在层表最底层(零内部依赖),
    stamps 在 providers。格式一致性靠这条注释与测试钉住:
    两者都是 ``YYYY-MM-DDTHH:MM:SS.mmm``。
    """
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass
class TeamTask:
    """一个任务。落盘形状见详规 §2.2:{id, title, state, assignee, deps, result, note}。

    ``result`` 双职:finish 写结论、fail 写失败原因(表里的"写 reason"落在这里)——
    一个任务同一时刻只有一个"结果"语义,拆两个字段反而要约定"哪个为空算什么"。
    """

    id: str
    title: str
    state: str = "pending"
    assignee: str = ""
    deps: list[str] = field(default_factory=list)
    result: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "assignee": self.assignee,
            "deps": list(self.deps),
            "result": self.result,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> TeamTask:
        """从落盘形状重建。**形状不对抛 ValueError**——半截/被改坏的文件
        静默当成空板,会让 create 覆盖掉还有人在跑的任务。"""
        if not isinstance(raw, dict):
            raise ValueError("任务条目不是 JSON 对象")
        if "id" not in raw:
            raise ValueError("任务条目缺少 id")
        state = raw.get("state", "pending")
        if state not in STATES:
            raise ValueError(
                f"未知任务状态 {state!r}(合法:{'/'.join(STATES)})"
            )
        deps = raw.get("deps", [])
        if not isinstance(deps, list):
            raise ValueError(f"任务 {raw['id']} 的 deps 不是列表")
        return cls(
            id=str(raw["id"]),
            title=str(raw.get("title", "")),
            state=cast(str, state),
            assignee=str(raw.get("assignee", "")),
            deps=[str(item) for item in deps],
            result=str(raw.get("result", "")),
            note=str(raw.get("note", "")),
        )


@dataclass
class Board:
    """整块板。``next_id`` 单调递增,id 永不复用——复用会让"重派"与
    "新任务"在历史里无法区分(与 todo 同一条纪律)。"""

    updated_at: str = ""
    next_id: int = 1
    tasks: list[TeamTask] = field(default_factory=list)

    def find(self, task_id: str) -> TeamTask | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "next_id": self.next_id,
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Board:
        if not isinstance(raw, dict):
            raise ValueError("任务板文件不是 JSON 对象")
        tasks_raw = raw.get("tasks", [])
        if not isinstance(tasks_raw, list):
            raise ValueError("任务板文件的 tasks 不是列表")
        return cls(
            updated_at=str(raw.get("updated_at", "")),
            next_id=int(raw.get("next_id", len(tasks_raw) + 1)),
            tasks=[TeamTask.from_dict(item) for item in tasks_raw],
        )

    def render(self) -> str:
        """渲染给人与模型都易读的看板。进度行放最前——它是"一眼看全局"的口径。"""
        counts = {state: 0 for state in STATES}
        for task in self.tasks:
            counts[task.state] += 1
        summary = " / ".join(f"{state} {counts[state]}" for state in STATES)
        lines = [f"任务板:{len(self.tasks)} 个任务({summary})"]
        for task in self.tasks:
            line = f"  [{task.state:<7}] {task.id} {task.title}"
            if task.assignee:
                line += f" @{task.assignee}"
            if task.deps:
                line += " (依赖: " + "、".join(task.deps) + ")"
            if task.note:
                line += f" — {task.note}"
            if task.result:
                line += f" → {task.result}"
            lines.append(line)
        return "\n".join(lines)


def apply(
    board: Board,
    event: str,
    *,
    task_id: str = "",
    caller: str = "",
    title: str = "",
    deps: Sequence[str] = (),
    result: str = "",
    note: str = "",
    lead: str = "",
) -> TeamTask:
    """按迁移表执行一个事件,返回(可能新建的)任务并**原地更新** board。

    表里没有 (当前状态, 事件) → ``ValueError``;守卫不过 → 同样 ``ValueError``。
    消息携带 当前状态/事件/调用者 + 合法迁移摘要,模型看到就能自查纠错。
    """
    task = board.find(task_id)
    current = ABSENT if task is None else task.state
    key = (current, event)
    if key not in TRANSITIONS:
        raise ValueError(
            f"非法迁移:任务 {task_id or '(新)'} 当前状态 {current} "
            f"不允许执行 {event}(调用者 {caller or '未知'})。"
            f"合法迁移:{_legal_summary()}"
        )
    _check_guards(board, task, event, caller=caller, lead=lead, deps=deps)
    next_state = TRANSITIONS[key]
    if event == CREATE:
        created = TeamTask(id=f"t{board.next_id}", title=title, deps=list(deps))
        board.next_id += 1
        board.tasks.append(created)
        return created
    assert task is not None  # current != ABSENT 时必有:表里其余行的当前状态都是实态
    task.state = next_state
    if event == CLAIM:
        task.assignee = caller
    elif event in (FINISH, FAIL):
        task.result = result
    elif event == RECLAIM:
        # 清 assignee/result,留 note(强制接管的审计痕迹)。running 的 result
        # 本应为空,清掉是幂等的;fail 的 result(失败原因)必须清——重派后
        # 旧失败原因不属于新一次执行。
        task.assignee = ""
        task.result = ""
        task.note = note
    return task


def _check_guards(
    board: Board,
    task: TeamTask | None,
    event: str,
    *,
    caller: str,
    lead: str,
    deps: Sequence[str],
) -> None:
    """迁移表各行的守卫(§2.1「守卫」列)。失败抛 ValueError,与非法迁移同形。"""
    if event == CREATE:
        _require_deps_success(board, deps)
        return
    assert task is not None
    if event == CLAIM:
        if not caller:
            raise ValueError(f"claim 需要调用者身份(任务 {task.id})。")
        if task.assignee:
            raise ValueError(
                f"任务 {task.id} 已被 {task.assignee} 认领,"
                f"调用者 {caller} 不能再 claim。"
            )
        _require_deps_success(board, task.deps)
    elif event in (FINISH, FAIL):
        if caller != task.assignee:
            raise ValueError(
                f"任务 {task.id} 由 {task.assignee or '(无)'} 认领,"
                f"调用者 {caller or '未知'} 不能 {event}"
                "(板纪律:非 assignee 不得改认领中的任务,含主 agent)。"
            )
    elif event == RECLAIM:
        if not lead or caller != lead:
            raise ValueError(
                f"reclaim 仅 lead 可调(lead={lead or '未设置'},"
                f"调用者 {caller or '未知'})。"
            )


def _require_deps_success(board: Board, deps: Sequence[str]) -> None:
    """守卫:依赖全部 success。不存在的依赖 id 直接拒绝——
    否则这条任务永远无法被 claim,而且没有任何一步报错。"""
    for dep_id in deps:
        dep = board.find(dep_id)
        if dep is None:
            raise ValueError(f"依赖 {dep_id} 不存在(板上没有这个任务 id)。")
        if dep.state != "success":
            raise ValueError(
                f"依赖 {dep_id} 尚未完成(当前 {dep.state}),"
                "须全部 success 才能继续。"
            )


def _legal_summary() -> str:
    return ";".join(
        f"{state}+{event}→{next_state}"
        for (state, event), next_state in TRANSITIONS.items()
    )
