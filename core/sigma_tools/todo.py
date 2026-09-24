"""``todo`` 工具：任务清单的创建、推进与查看。

需求（星辰，2026-09-23）：把复杂长程任务拆解成短程任务清单，持久化在工作区，
模型按计划依次执行；三状态 pending / running / completed 支持切换与断点重续；
配合 loop 层的 steering（连续 N 轮未查看就提醒），防止长任务跑偏。

为什么文件放 ``.sigma/todo.json``
    ``.sigma/`` 在影子 checkpoint 的 ``BUILTIN_EXCLUDES`` 里（checkpoint.py），
    回滚（``reset --hard``）**碰不到它**——断点重续因此天然成立。
    放普通路径的后果：restore 会把"回滚点之后新建"的清单文件当垃圾删掉，
    模型恢复会话后计划凭空消失，而没有任何一步报错。

为什么 create/update 的返回都带全清单
    模型每一步操作后都重新看到当前状态，"操作即刷新"。
    这与 steering 互为双保险：Guide（提示词引导）管一次做对，
    Sensor（steering 计数）兜失误——只用其中任何一个都会坏掉。

为什么 title 不给 update
    改标题 = 语义上重建计划。两个写入口都能改同一字段，"现在清单里到底是什么"
    就要靠约定保证——约定会漏。要改标题：create + overwrite=true 全量重建。
    与 edit「多匹配必须拒绝」同源：**有歧义宁可报错，不要猜**。

为什么要有第四个动作 ``revise``（星辰 2026-09-24）
    开工时的拆解是**基于当时信息的推测**；执行到第 3 步时，模型对剩下部分的
    理解比开工时深得多——后续任务的划分与难度都会变。若清单只能"照最初版本
    执行完"，模型就被自己开工时的无知锁死了。

    ``revise`` 只换**未完成部分**（pending），已完成与正在跑的一条都不碰：

    - completed 是**已发生的事实**，改写它等于伪造历史；
    - running 是**正在做的事**，凭空让它消失会让"至多一条 running"失去意义；
    - 只有 pending 是"尚未发生的计划"，也是唯一可被新信息推翻的部分。

    为什么不是扩展 create / update
        create = 全量重建（要 ``overwrite=true``，语义是推翻重来，会抹掉历史）；
        update = 改单条（不能增删、不能重排）。
        revise 是**第三个语义**，凑进任意一个都让"我到底在改什么"靠约定保证，
        而约定会漏——与上面"title 不给 update"是同一条判据。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai import stamps
from sigma_ai.messages import TextBlock

#: 清单文件的固定落点（相对工作区根）。工具自己的账本，不是用户路径，
#: 所以 ``.sigma`` 目录不存在时**自动创建**——这里没有"路径写错"的歧义风险
#: （与 write 工具"不自动建父目录"不是同一条纪律，别混用）。
TODO_RELATIVE = ".sigma/todo.json"

Status = Literal["pending", "running", "completed"]
_ALL_STATUSES: tuple[str, ...] = ("pending", "running", "completed")

#: 难度档位名。**与子 agent 的轮数预算同词汇**（``SubAgentRounds``）——
#: 清单里标了难度的任务，派给 sub_agent 时直接用同一档，不用重新猜。
Difficulty = Literal["low", "medium", "high"]


class TodoParams(BaseModel):
    """四个动作共用一个参数模型，按 ``action`` 区分必填。schema 进常驻区（D4），说明必须短。"""

    action: Literal["create", "update", "revise", "list"] = Field(
        description="create=建清单；update=改单条；revise=重排后续 pending；list=查看。"
    )
    goal: str = Field(default="", description="create 必填：总体目标。")
    items: list[str] = Field(
        default_factory=list, description="create/revise 必填：任务标题列表（≥1 条）。"
    )
    overwrite: bool = Field(
        default=False, description="create 时清单已存在则报错；确认重建传 true。"
    )
    id: int = Field(default=0, description="update 必填：任务编号。")
    status: Status | None = Field(
        default=None, description="update 可选：pending / running / completed。"
    )
    detail: str | None = Field(
        default=None, description="update 可选：补充说明。"
    )
    difficulty: list[Difficulty] | None = Field(
        default=None,
        description="create/revise 可选：与 items 按位置对应的难度（low/medium/high）；"
        "长度必须与 items 一致。",
    )


class _TodoData(BaseModel):
    """落盘的数据形状。``next_id`` 单调递增，id 永不复用——
    复用 id 会让"返工重开"和"新任务"在历史里无法区分。"""

    goal: str
    updated_at: str
    next_id: int
    items: list[dict[str, Any]]


def _render(data: _TodoData) -> str:
    """渲染给人与模型都易读的清单。进度行放最前——它是 steering 提醒里也引用的口径。"""
    done = sum(1 for it in data.items if it["status"] == "completed")
    running = sum(1 for it in data.items if it["status"] == "running")
    total = len(data.items)
    lines = [f"目标: {data.goal}", f"进度: {done}/{total} completed, {running} running"]
    for it in data.items:
        line = f"  [{it['status']:<9}] #{it['id']} {it['title']}"
        if it.get("difficulty"):
            line += f" 【{_DIFFICULTY_LABEL[it['difficulty']]}】"
        if it["detail"]:
            line += f" — {it['detail']}"
        lines.append(line)
    return "\n".join(lines)


#: 渲染用的难度标签。写中文而不是原样印 low/medium/high——看清单的是模型，
#: 中文标签在一行里更好扫，且不会与英文标题混在一起看错。
_DIFFICULTY_LABEL = {"low": "低", "medium": "中", "high": "高"}


def _zip_difficulty(
    items: list[str], difficulty: list[Difficulty] | None
) -> list[str | None]:
    """把难度按位置配到标题上。**长度不一致直接报错**——

    平行数组是脆弱设计（两列对不上就全错），校验必须在这里做死：
    宁可让这一调用失败，也不要让"第 3 条的难度"悄悄落在第 2 条上。
    """
    if difficulty is None:
        return [None] * len(items)
    if len(difficulty) != len(items):
        raise ValueError(
            f"difficulty 有 {len(difficulty)} 条，items 有 {len(items)} 条，"
            "必须一一对应（不传 difficulty 则全部不标）。"
        )
    return [cast(str, level) for level in difficulty]


def _check_transition(old: str, new: str, task_id: int) -> str | None:
    """校验状态流转。返回 None 表示合法；返回字符串是给模型看的拒绝理由。"""
    if old == new:
        return None  # 幂等：断点重续时会重复 update 同一状态，这是正常操作
    forward = {"pending": "running", "running": "completed"}
    if forward.get(old) == new:
        return None
    if old == "completed" and new == "pending":
        return None  # 返工重开：显式允许，返回里会如实报告
    return (
        f"#{task_id} 不允许从 {old} 直接改到 {new}。"
        "合法流转：pending → running → completed；completed → pending 表示返工重开。"
    )


class TodoTool(BaseTool):
    """任务清单工具。写工具（改 ``.sigma/todo.json``），批次中严格顺序执行。

    ``relative_path``（P4 task 工具）：账本落点，默认不变。子 agent 的账本
    换成 ``.sigma/todo-<派生id>.json``——两个 agent 共享一个账本会让
    "至多一条 running"被静默破坏（主 agent 正在跑 #3，子 agent 把它标
    completed，状态机毫无察觉）。**账本随 agent 走，一个 agent 一本账。**
    """

    name = "todo"
    description = (
        "任务清单（.sigma/todo.json）。长任务先 create 拆解，"
        "每完成一步 update 状态，同时只允许一条 running；"
        "执行中发现后续划分或难度判断不对，用 revise 重排未开始的 pending 部分"
        "（已完成与正在跑的不动）。流转：pending→running→completed；"
        "completed→pending 为返工。items 可配 difficulty（low/medium/high）。"
    )
    read_only = False

    def __init__(self, relative_path: str = TODO_RELATIVE) -> None:
        self._relative_path = relative_path

    # ------------------------------------------------------------------
    # 账本读写（实例方法：路径随实例走——子 agent 换独立账本不改这里一行）
    # ------------------------------------------------------------------

    def _todo_path(self, ctx: ToolContext) -> Path:
        return ctx.workspace_root / self._relative_path

    def _load(self, ctx: ToolContext) -> _TodoData | None:
        """读清单。文件不存在返回 None；**损坏时报错路径由调用方走 is_error**，
        这里抛 ValueError——宁可崩不要错：半截 JSON 静默当成"没有清单"
        会让模型 create 覆盖掉还有三条没做完的计划。"""
        path = self._todo_path(ctx)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("清单文件不是 JSON 对象")
        return _TodoData.model_validate(raw)

    def _save(self, ctx: ToolContext, data: _TodoData) -> None:
        path = self._todo_path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            data.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @property
    def params(self) -> type[BaseModel]:
        return TodoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = cast(TodoParams, args)
        try:
            if params.action == "create":
                return self._create(params, ctx)
            if params.action == "update":
                return self._update(params, ctx)
            if params.action == "revise":
                return self._revise(params, ctx)
            return self._list(ctx)
        except ValueError as exc:
            # 损坏文件 / 数据形状不对：给模型看原因，不吞掉。
            return ToolResult(
                content=[TextBlock(text=f"todo 清单文件无法解析：{exc}")],
                details={"action": params.action, "corrupt": True},
                is_error=True,
            )

    # ------------------------------------------------------------------

    def _create(self, params: TodoParams, ctx: ToolContext) -> ToolResult:
        if not params.goal.strip():
            return ToolResult(
                content=[TextBlock(text="create 需要 goal（总体目标一句话）。")],
                details={"action": "create", "missing": "goal"},
                is_error=True,
            )
        if not params.items:
            return ToolResult(
                content=[
                    TextBlock(text="create 需要 items（至少 1 条拆解后的任务）。")
                ],
                details={"action": "create", "missing": "items"},
                is_error=True,
            )
        existing = self._load(ctx)
        if existing is not None and not params.overwrite:
            return ToolResult(
                content=[
                    TextBlock(
                        text="清单已存在（防误覆盖，未改动）。当前清单：\n"
                        + _render(existing)
                        + "\n要推进度用 update；确认整体重建才传 overwrite=true。"
                    )
                ],
                details={"action": "create", "already_exists": True},
                is_error=True,
            )

        levels = _zip_difficulty(params.items, params.difficulty)
        data = _TodoData(
            goal=params.goal.strip(),
            updated_at=stamps.now(),
            next_id=len(params.items) + 1,
            items=[
                {
                    "id": index,
                    "title": title.strip(),
                    "detail": "",
                    "status": "pending",
                    "difficulty": level,
                }
                for index, (title, level) in enumerate(
                    zip(params.items, levels, strict=False), start=1
                )
                if title.strip()
            ],
        )
        if not data.items:
            return ToolResult(
                content=[TextBlock(text="items 里没有有效标题（不能全是空白）。")],
                details={"action": "create", "missing": "items"},
                is_error=True,
            )
        data.next_id = len(data.items) + 1
        self._save(ctx, data)
        note = "已重建" if existing is not None else "已创建"
        return ToolResult(
            content=[TextBlock(text=f"{note}任务清单（{len(params.items)} 条，全部 pending）：\n" + _render(data))],
            details={"action": "create", "items": len(params.items), "overwritten": existing is not None},
        )

    def _revise(self, params: TodoParams, ctx: ToolContext) -> ToolResult:
        """重排**后续**（pending）任务：保留已完成与正在跑的，只换未开始的部分。

        这是"计划随执行进化"的落点。开工时的拆解基于当时的信息，
        执行到中途模型对剩下部分的理解更深——后续任务的划分与难度都该能改。
        """
        data = self._load(ctx)
        if data is None:
            return ToolResult(
                content=[
                    TextBlock(
                        text="还没有任务清单。先 todo(action=\"create\", goal=..., items=[...]) 建计划。"
                    )
                ],
                details={"action": "revise", "missing_file": True},
                is_error=True,
            )
        if not params.items:
            return ToolResult(
                content=[TextBlock(text="revise 需要 items（新的后续任务，≥1 条）。")],
                details={"action": "revise", "missing": "items"},
                is_error=True,
            )
        levels = _zip_difficulty(params.items, params.difficulty)

        kept = [it for it in data.items if it["status"] != "pending"]
        replaced = [it for it in data.items if it["status"] == "pending"]
        if not replaced:
            return ToolResult(
                content=[
                    TextBlock(
                        text="没有可调整的后续任务（清单里没有 pending 条目）。"
                        "当前清单：\n" + _render(data)
                    )
                ],
                details={"action": "revise", "no_pending": True},
                is_error=True,
            )

        fresh: list[dict[str, Any]] = []
        for title, level in zip(params.items, levels, strict=False):
            if not title.strip():
                continue
            fresh.append(
                {
                    "id": data.next_id,
                    "title": title.strip(),
                    "detail": "",
                    "status": "pending",
                    "difficulty": level,
                }
            )
            data.next_id += 1
        if not fresh:
            return ToolResult(
                content=[TextBlock(text="items 里没有有效标题（不能全是空白）。")],
                details={"action": "revise", "missing": "items"},
                is_error=True,
            )

        # 顺序 = 执行顺序：已完成 + 正在跑 + 新的后续。
        data.items = kept + fresh
        data.updated_at = stamps.now()
        self._save(ctx, data)

        # 丢弃必须可见：被换掉的那些条目要逐条报出来，不能悄悄消失。
        dropped = "、".join(f"#{it['id']} {it['title']}" for it in replaced)
        return ToolResult(
            content=[
                TextBlock(
                    text=(
                        f"已重排后续任务：替换 {len(replaced)} 条（{dropped}），"
                        f"新增 {len(fresh)} 条。已完成与正在跑的 {len(kept)} 条未改动。\n"
                        + _render(data)
                    )
                )
            ],
            details={
                "action": "revise",
                "replaced": len(replaced),
                "added": len(fresh),
                "kept": len(kept),
            },
        )

    def _update(self, params: TodoParams, ctx: ToolContext) -> ToolResult:
        data = self._load(ctx)
        if data is None:
            return ToolResult(
                content=[
                    TextBlock(
                        text="还没有任务清单。先 todo(action=\"create\") 拆解计划，或 list 查看说明。"
                    )
                ],
                details={"action": "update", "missing_file": True},
                is_error=True,
            )
        target = next((it for it in data.items if it["id"] == params.id), None)
        if target is None:
            return ToolResult(
                content=[
                    TextBlock(
                        text=f"没有 id={params.id} 的任务。当前清单：\n" + _render(data)
                    )
                ],
                details={"action": "update", "bad_id": params.id},
                is_error=True,
            )

        old_status = cast(Status, target["status"])
        if params.status is not None:
            reason = _check_transition(old_status, params.status, params.id)
            if reason is not None:
                return ToolResult(
                    content=[TextBlock(text=reason + "\n当前清单：\n" + _render(data))],
                    details={"action": "update", "bad_transition": [old_status, params.status]},
                    is_error=True,
                )
            # 至多一条 running：「依次执行」的机器化。
            # 报错而不是自动挂起旧的——有歧义宁可报错，不要猜（详规 3.5）。
            if params.status == "running":
                conflict = next(
                    (
                        it
                        for it in data.items
                        if it["status"] == "running" and it["id"] != params.id
                    ),
                    None,
                )
                if conflict is not None:
                    return ToolResult(
                        content=[
                            TextBlock(
                                text=f"#{conflict['id']}（{conflict['title']}）还在 running，"
                                f"同一时刻只允许一条 running。先把它 update 成 completed，"
                                f"或确实要放弃它时 update 成 completed 前先在 detail 里说明。\n"
                                f"当前清单：\n" + _render(data)
                            )
                        ],
                        details={
                            "action": "update",
                            "conflict_id": conflict["id"],
                        },
                        is_error=True,
                    )
            target["status"] = params.status
        if params.detail is not None:
            target["detail"] = params.detail

        data.updated_at = stamps.now()
        self._save(ctx, data)
        change = (
            f"#{params.id} 状态 {old_status} → {target['status']}。"
            if params.status is not None and params.status != old_status
            else f"#{params.id} 状态保持 {old_status}（幂等）。"
            if params.status is not None
            else f"#{params.id} 已更新说明。"
        )
        return ToolResult(
            content=[TextBlock(text=f"清单已更新。{change}\n" + _render(data))],
            details={
                "action": "update",
                "id": params.id,
                "status": target["status"],
            },
        )

    def _list(self, ctx: ToolContext) -> ToolResult:
        data = self._load(ctx)
        if data is None:
            return ToolResult(
                content=[
                    TextBlock(
                        text="还没有任务清单。长任务建议先 todo(action=\"create\", goal=..., items=[...]) "
                        "拆解成短程任务再动手；单步小任务可不建清单。"
                    )
                ],
                details={"action": "list", "missing_file": True},
            )
        return ToolResult(
            content=[TextBlock(text=_render(data))],
            details={"action": "list", "items": len(data.items)},
        )
