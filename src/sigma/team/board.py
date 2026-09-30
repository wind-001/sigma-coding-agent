"""团队任务板状态机:迁移表即代码,纯逻辑、零 I/O、零 sigma 内部依赖(纯 stdlib)。

P4-团队任务详规 §2.1(v3,星辰拍板 2026-09-30):七态
pending / running / blocked / success / fail / dead / cancelled;
迁移表(当前状态 × 事件 → 次态)就是下面那张字面量 dict——**表即规范**,
G-TEAM-1 逐行参数化钉住:改表不补测试(或反之)当场红。

为什么独立成包且只用 stdlib
    协作域(状态机 / 存储 / 信箱 / 扫描器)是规则密集而框架知识为零的代码:
    它不需要知道 ToolContext、pydantic 或任何 provider——给它一个 Board 和
    一个事件,它给答案。(代价:时间戳/时钟助手必须在包内自足,见
    :func:`now_stamp`。)

角色即守卫(v3,星辰设计评审)
    守卫列的本质是**角色声明**:worker 发 claim/finish/fail/heartbeat,
    lead 发 create/reclaim/abandon/cancel,系统扫描器发 lease_expired /
    dep_succeeded。物理分权在 :mod:`sigma.team.worker_board` /
    :mod:`sigma.team.lead_board` / :mod:`sigma.team.scanner`——本模块的
    运行时守卫是第二道防线(调用方传错身份照样拦)。

非法迁移一律 ``ValueError``(携带 当前状态/事件/调用者)——**宁可崩不要错**:
    静默吞掉非法迁移,"板上到底是什么状态"就只能靠约定保证,而约定会漏。
    调用方(tools/builtin/team.py 薄壳)负责把 ValueError 转成 is_error 的
    ToolResult(架构 4.2:工具失败返回 is_error,不向上抛)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator

#: 合法状态,恰七值(v3)。顺序 = 生命周期序(渲染与文档同序)。
STATES: Final[tuple[str, ...]] = (
    "pending",
    "running",
    "blocked",
    "success",
    "fail",
    "dead",
    "cancelled",
)

#: create 事件的"当前状态":板上还没有这个 id。用中文原文,报错可直读详规的表。
ABSENT: Final[str] = "(不存在)"

#: 事件名。与工具 board 子操作**同名**——薄壳按名字转发,不设第二套词汇。
#: heartbeat / worker_lost / lease_expired / dep_succeeded 是**引擎与扫描器的
#: 内部事件**,不进任何工具面(角色分权,详规 §2)。
CREATE: Final[str] = "create"
CLAIM: Final[str] = "claim"
FINISH: Final[str] = "finish"
FAIL: Final[str] = "fail"
RECLAIM: Final[str] = "reclaim"
ABANDON: Final[str] = "abandon"
CANCEL: Final[str] = "cancel"
HEARTBEAT: Final[str] = "heartbeat"
WORKER_LOST: Final[str] = "worker_lost"
LEASE_EXPIRED: Final[str] = "lease_expired"
DEP_SUCCEEDED: Final[str] = "dep_succeeded"

#: 系统调用者前缀:扫描器与引擎以系统身份触发内部事件(角色分权:它们不是 agent)。
SYSTEM_SCANNER: Final[str] = "system:scanner"
SYSTEM_ENGINE: Final[str] = "system:engine"

#: 迁移表(§2.1):(当前状态, 事件) → 次态。**这张 dict 就是规范本身**。
#: create 的次态由 deps 守卫分流(全 success → pending;未就绪 → blocked),
#: 表里登记主路径 pending。success/dead/cancelled 是吸收态,无出边。
TRANSITIONS: Final[dict[tuple[str, str], str]] = {
    (ABSENT, CREATE): "pending",
    ("pending", CLAIM): "running",
    ("running", FINISH): "success",
    ("running", FAIL): "fail",
    ("running", HEARTBEAT): "running",
    ("running", WORKER_LOST): "pending",
    ("running", LEASE_EXPIRED): "pending",
    ("running", CANCEL): "cancelled",
    ("running", RECLAIM): "pending",
    ("fail", RECLAIM): "pending",
    ("fail", ABANDON): "dead",
    ("blocked", DEP_SUCCEEDED): "pending",
    ("blocked", CANCEL): "cancelled",
    ("pending", CANCEL): "cancelled",
}


def now_stamp() -> str:
    """包内默认时间源:本地时间、毫秒,格式与 ``sigma.providers.stamps.now`` 一致。

    为什么不 import stamps:``sigma.team`` 在层表最底层(零内部依赖),
    stamps 在 providers。格式一致性靠这条注释与测试钉住:
    两者都是 ``YYYY-MM-DDTHH:MM:SS.mmm``。
    """
    return datetime.now().isoformat(timespec="milliseconds")


class TeamTask(BaseModel):
    """一个任务。落盘形状见详规 §2.2(v3 增补 attempts/lease_deadline/creator)。

    ``result`` 双职:finish 写结论、fail 写失败原因(表里的"写 reason"落在这里)——
    一个任务同一时刻只有一个"结果"语义,拆两个字段反而要约定"哪个为空算什么"。
    ``lease_deadline`` 是**注入时钟的秒数**(不是 epoch、不是 ISO 串)——
    lease 比较发生在同一次注入时钟的刻度里,可读性由 render/审计另管。

    载体是 ``BaseModel``(AGENTS.md 第 3 条三问判定,Review-2026-09-30 B3):
    ①不生成工具 schema,但 ②**落盘**(board.json)且 ③**校验外部输入**
    (被改坏的板文件、被手改的 attempts)——②③ 任一成立即用 pydantic,
    v1 的"dataclass + 手写 from_dict 校验"是这条规则的违规实例。
    """

    id: str
    title: str
    state: str = "pending"
    assignee: str = ""
    deps: list[str] = Field(default_factory=list)
    result: str = ""
    note: str = ""
    creator: str = ""
    attempts: int = 0
    lease_deadline: float = 0.0

    @field_validator("state")
    @classmethod
    def _known_state(cls, value: str) -> str:
        """状态必须落在 :data:`STATES` 里。

        **刻意用 str + validator 而不是 ``Literal[...]``**:Literal 的报错由
        pydantic 生成(英文 + 落点 loc/type),而这里的报错要能直读详规的
        状态表——"未知任务状态 'claimed'(合法:pending/running/...)"
        比 ``Input should be 'pending', 'running', ...`` 对模型有用得多。
        """
        if value not in STATES:
            raise ValueError(f"未知任务状态 {value!r}(合法:{'/'.join(STATES)})")
        return value

    def to_dict(self) -> dict[str, Any]:
        """落盘形状。**字段名不动**(旧 board.json 必须还能读回)。"""
        return self.model_dump()

    @classmethod
    def from_dict(cls, raw: Any) -> TeamTask:
        """从落盘形状重建。**形状不对抛 ValueError**——半截/被改坏的文件
        静默当成空板,会让 create 覆盖掉还有人在跑的任务。

        两层:先挡"根本不是对象 / 连 id 都没有"这两类(它们的文案比
        pydantic 的更有指向性),其余交给 pydantic 校验。
        """
        if not isinstance(raw, dict):
            raise ValueError("任务条目不是 JSON 对象")
        if "id" not in raw:
            raise ValueError("任务条目缺少 id")
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"任务条目不合法:{exc}") from exc


class Board(BaseModel):
    """整块板。``next_id`` 单调递增,id 永不复用——复用会让"重派"与
    "新任务"在历史里无法区分(与 todo 同一条纪律)。

    载体同 :class:`TeamTask`(落盘 + 外部输入校验 ⇒ pydantic)。
    """

    updated_at: str = ""
    next_id: int = 1
    tasks: list[TeamTask] = Field(default_factory=list)
    #: 重试上限(v3 迁移表第 10/11 行):reclaim 守卫 attempts < max_attempts,
    #: 达上限由扫描器自动 abandon 进 dead。落盘随板走——它是板的一部分配置。
    max_attempts: int = 3

    def find(self, task_id: str) -> TeamTask | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, raw: Any) -> Board:
        if not isinstance(raw, dict):
            raise ValueError("任务板文件不是 JSON 对象")
        tasks_raw = raw.get("tasks", [])
        if not isinstance(tasks_raw, list):
            raise ValueError("任务板文件的 tasks 不是列表")
        # next_id 缺省 = 现有任务数 + 1(与 v1 同口径):老文件没有这个字段时,
        # 新 id 不能与已有 id 撞车。
        payload = dict(raw)
        payload.setdefault("next_id", len(tasks_raw) + 1)
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"任务板文件不合法:{exc}") from exc

    def render(self) -> str:
        """渲染给人与模型都易读的看板。进度行放最前——它是"一眼看全局"的口径。"""
        counts = {state: 0 for state in STATES}
        for task in self.tasks:
            counts[task.state] += 1
        summary = " / ".join(f"{state} {counts[state]}" for state in STATES)
        lines = [f"任务板:{len(self.tasks)} 个任务({summary})"]
        for task in self.tasks:
            line = f"  [{task.state}] {task.id} {task.title}"
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
    now: float = 0.0,
    lease_ttl: float = 0.0,
) -> TeamTask:
    """按迁移表执行一个事件,返回(可能新建的)任务并**原地更新** board。

    表里没有 (当前状态, 事件) → ``ValueError``;守卫不过 → 同样 ``ValueError``。
    消息携带 当前状态/事件/调用者 + 合法迁移摘要,模型看到就能自查纠错。
    ``now`` / ``lease_ttl`` 只被 claim/heartbeat 消费(lease 秒数刻度);
    create 的次态由 deps 守卫分流(全 success → pending,未就绪 → blocked)。
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
        # v3:create 的 deps 守卫只要求**存在**;未就绪 → blocked 挂起,
        # dep_succeeded 扫描放行(详规 v3 迁移表第 2 行)。
        ready = deps_all_success(board, deps)
        state = "pending" if ready else "blocked"
        created = TeamTask(
            id=f"t{board.next_id}",
            title=title,
            deps=list(deps),
            state=state,
            creator=caller,
        )
        board.next_id += 1
        board.tasks.append(created)
        return created
    assert task is not None  # current != ABSENT 时必有:表里其余行的当前状态都是实态
    task.state = next_state
    if event == CLAIM:
        task.assignee = caller
        task.lease_deadline = now + lease_ttl
    elif event == HEARTBEAT:
        task.lease_deadline = now + lease_ttl
    elif event == FINISH:
        task.result = result
        task.lease_deadline = 0.0
    elif event == FAIL:
        task.result = result
        task.attempts += 1
        task.lease_deadline = 0.0
    elif event in (WORKER_LOST, LEASE_EXPIRED):
        # 两通道同一去向:事件驱动(派发器感知)与时间驱动(扫描器)。
        # attempts 都递增——重试上限对两条通道一视同仁。
        task.assignee = ""
        task.attempts += 1
        task.lease_deadline = 0.0
        task.note = note or task.note
    elif event == RECLAIM:
        # 清 assignee/result,留 note(强制接管的审计痕迹)。running 的 result
        # 本应为空,清掉是幂等的;fail 的 result(失败原因)必须清——重派后
        # 旧失败原因不属于新一次执行。
        task.assignee = ""
        task.result = ""
        task.note = note
    elif event == CANCEL:
        task.note = note or task.note
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
    """迁移表各行的守卫(§2.1「守卫」列)。失败抛 ValueError,与非法迁移同形。

    角色总纲(详规 §2):worker 发 claim/finish/fail/heartbeat;lead 发
    create/reclaim/abandon/cancel;lease_expired/dep_succeeded/worker_lost
    是系统事件,调用者必须是 ``system:*``(角色分权的第二道防线)。
    """
    if event == CREATE:
        _require_deps_exist(board, deps)
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
    elif event in (FINISH, FAIL, HEARTBEAT):
        if caller != task.assignee:
            raise ValueError(
                f"任务 {task.id} 由 {task.assignee or '(无)'} 认领,"
                f"调用者 {caller or '未知'} 不能 {event}"
                "(板纪律:非 assignee 不得操作认领中的任务,含主 agent)。"
            )
    elif event == RECLAIM:
        if not lead or caller != lead:
            raise ValueError(
                f"reclaim 仅 lead 可调(lead={lead or '未设置'},"
                f"调用者 {caller or '未知'})。"
            )
        assert task is not None
        if task.attempts >= board.max_attempts:
            raise ValueError(
                f"任务 {task.id} attempts={task.attempts} 已达上限"
                f"({board.max_attempts}),reclaim 被拒——用 abandon 终结,"
                "或先调高板上限。重试上限对 lead 一视同仁,否则形同虚设。"
            )
    elif event == ABANDON:
        if caller != lead and not caller.startswith("system:"):
            raise ValueError(
                f"abandon 仅 lead 或扫描器可调(调用者 {caller or '未知'})。"
            )
    elif event in (WORKER_LOST, LEASE_EXPIRED, DEP_SUCCEEDED):
        if not caller.startswith("system:"):
            raise ValueError(
                f"{event} 是系统事件,仅扫描器/引擎可触发(调用者 {caller or '未知'})。"
            )
    elif event == CANCEL:
        # pending/blocked:创建者或 lead;running:仅 lead(正在执行的工作
        # 只有管理者能砍,worker 自己放弃走 fail)。
        if task.state == "running":
            if not lead or caller != lead:
                raise ValueError(
                    f"取消 running 任务仅 lead 可调(lead={lead or '未设置'},"
                    f"调用者 {caller or '未知'})。"
                )
        else:
            if caller != lead and caller != task.creator and not caller.startswith("system:"):
                raise ValueError(
                    f"取消 {task.state} 任务需要创建者或 lead"
                    f"(创建者 {task.creator or '(无)'},调用者 {caller or '未知'})。"
                )


def _require_deps_exist(board: Board, deps: Sequence[str]) -> None:
    """create 守卫:依赖必须存在(id 可查)——不存在的依赖让任务永远无法
    被放行,而且没有任何一步报错。依赖未就绪**不再拒绝**,转 blocked(v3)。"""
    for dep_id in deps:
        if board.find(dep_id) is None:
            raise ValueError(f"依赖 {dep_id} 不存在(板上没有这个任务 id)。")


def _require_deps_success(board: Board, deps: Sequence[str]) -> None:
    """claim 的防御性守卫:normal 路径下 blocked→pending 已保证依赖就绪;
    保留它是为了直接摆出 pending 板时的回退防御(与详规同判据)。"""
    for dep_id in deps:
        dep = board.find(dep_id)
        if dep is None:
            raise ValueError(f"依赖 {dep_id} 不存在(板上没有这个任务 id)。")
        if dep.state != "success":
            raise ValueError(
                f"依赖 {dep_id} 尚未完成(当前 {dep.state}),"
                "须全部 success 才能继续。"
            )


def deps_all_success(board: Board, deps: Sequence[str]) -> bool:
    """依赖是否全部 ``success``。

    公开而非私有:扫描器(:mod:`sigma.team.scanner`)的放行判据与 create 的
    分流判据**必须是同一条**——两份实现会在某次改判据时漂移,而症状是
    "板上有任务永远不动",离根因很远。
    """
    return all(
        (dep := board.find(dep_id)) is not None and dep.state == "success"
        for dep_id in deps
    )


def _legal_summary() -> str:
    return ";".join(
        f"{state}+{event}→{next_state}"
        for (state, event), next_state in TRANSITIONS.items()
    )
