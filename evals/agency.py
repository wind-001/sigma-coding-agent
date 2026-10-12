"""agency 评测轴：从**既有落盘数据**算「推进能力」指标（阶段 0 测量装置）。

> 本文件不新增任何遥测设施。它只读两种**已经存在**的数据：
>
> 1. ``evals/reports/task_<id>_<profile>.json``——task_runner 的报告
>    （``result`` = TurnResult 摘要，``verdict`` = 判定器结论）；
> 2. ``<sessions_dir>/<task>_<profile>.trace.jsonl``——``observability/trace.py``
>    的 TraceHook 落盘（``llm_requested`` / ``llm_end`` / ``tool_start`` /
>    ``tool_end`` / ``injected`` / ``turn_end``）。
>
> trace 与会话 JSONL 的落盘条件是**调用方传了 ``tree``**（sdk.py：
> ``enable_trace and tree is not None``）。所以跑基线时必须让 task_runner
> 传 tree（``--sessions-dir``），否则指标 3/4 无定义——缺数据如实报
> ``None``，**不静默变 0**。

**为什么先造这把尺子再选机制**（开工指令 P7）：星辰没有任何 sigma 与参照系
的对比记录，六条症状是体感。照体感改 runtime 就是生产名义门槛。这把尺子
回答一个问题：**关掉/打开某个推进机制，任务会不会提前收束或走得更保守**
（CONTEXT.md「推进机制」词条的判据）。

五个指标（口径全文见各 dataclass 的 docstring）

    M1 真收束率 / 谎报完成率   completed 的任务里 verdict 通过/未过的比例
                              ——症状 A「一收手就结束、不自己发现没做完」的对偶
    M2 卡点自陈率             verdict 未过的任务里 stopped+reason 非空的比例
                              ——「卡住时如实汇总，还是静默收尾」
    M3 未查证即结论率         首个 write/edit 之前没有任何 read/grep/task 的比例
                              ——「下结论前不查证」（bash 读写混合，不计入任一类）
    M4 后续步骤自发起率       rounds>=2 的比例
                              ——症状 E「每条任务都要人喂，自己不接着往下推」
    M5 收束轮数               completed 任务的 rounds 中位数（辅助，轮数预算讨论用）

**样本与推断纪律**：本轴与 ax 能力评测同口径——**每任务 1 次采样**，
只报点估计与样本量，不给置信区间外的结论（温度 0 但服务端非确定，
syn-004 同档重跑曾有 8 轮 vs 16 轮差异）。要方差信息须每题 N>=3，
那是后续批次的事，本文件不假装有。

用法
    ./.venv/Scripts/python.exe evals/agency.py                       # 扫全部报告
    ./.venv/Scripts/python.exe evals/agency.py --profile B1          # 只要 B1 基线臂
    ./.venv/Scripts/python.exe evals/agency.py --tasks ax101,ax102   # 只要指定任务
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel

EVALS = Path(__file__).resolve().parent
REPO = EVALS.parent
REPORTS = EVALS / "reports"

#: 查证类工具：**看世界**的动作。判据是"它只读不改"——read/grep 纯读，
#: task 派子 agent 查证（主会话自己不碰文件）。
#: **bash 刻意不在任何一类**：它读写混合，静态无法区分
#: （``bash("cat x")`` 是读、``bash("echo > x")`` 是写），把它算进任一桶
#: 都会让指标失去可证伪性。代价：查证被少算——所以 M3 是未查证率的**上界**。
EVIDENCE_TOOLS = frozenset({"read", "grep", "task"})

#: 写类工具：**改世界**的动作（"下结论"在工具语境里的形态）。
#: write/edit 都是落盘修改；bash 同上不计入。
WRITE_TOOLS = frozenset({"write", "edit"})


class TaskAgency(BaseModel):
    """一条任务一次 turn 的 agency 指标（行数据）。

    ``None`` 一律表示**无定义**（缺 trace / 该任务不适用），不是 0——
    这两个值在这把尺子上含义相反，混同就是制造幻觉。
    """

    task_id: str
    eval_profile: str
    # ---- 来自 task report（status/verdict/rounds）----
    status: str
    verdict_passed: bool | None
    rounds: int
    reason: str | None = None
    # ---- 来自 trace ----
    tool_sequence: list[str] | None = None
    # M1 收束点：模型自己收敛（completed）且客观判据（verdict）认可
    converged_clean: bool | None = None
    falsely_completed: bool | None = None
    # M2 卡点自陈：任务没做成（verdict 未过）时，是否以 stopped+reason 如实收尾
    stall_self_report: bool | None = None
    # M3 未查证即结论：有写类调用且首个写类调用前无任何查证类调用
    unverified_conclusion: bool | None = None
    # M4 自发推进：第 1 轮之后模型自己接着干（rounds>=2）
    self_continued: bool | None = None


def _git_commit() -> str:
    """当前提交号（计数纪律：数字与提交号绑定；detached/无 git 时如实记 unknown）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # noqa: BLE001 — 计数绑不上也不能让尺子跑不动
        pass
    return "unknown"


def _load_trace(path: Path) -> list[dict[str, object]] | None:
    """读 trace JSONL。文件不存在 = 该报告没落盘（旧报告/没传 --sessions-dir）。"""
    if not path.is_file():
        return None
    events: list[dict[str, object]] = []
    for line in path.read_text("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return events


def _tool_sequence(events: list[dict[str, object]]) -> list[str]:
    """按发生顺序取出全部工具名（``tool_start`` 事件）。

    顺序即调用顺序：loop 在一个批次里先发完所有 ToolStart 再执行，
    ToolStart 的相对次序保留模型的调用决策次序。
    """
    return [str(e.get("name")) for e in events if e.get("kind") == "tool_start"]


def _classify(sequence: list[str]) -> tuple[bool | None, bool | None]:
    """算 M3：``(未查证即结论, 是否适用)`` 的判定部。

    适用条件：任务里**有**写类调用（没有「下结论」动作就无从谈未查证）。
    判定：首个 write/edit 之前是否存在 read/grep/task。
    """
    writes = [i for i, name in enumerate(sequence) if name in WRITE_TOOLS]
    if not writes:
        return None, None
    first_write = writes[0]
    evidenced = any(name in EVIDENCE_TOOLS for name in sequence[:first_write])
    return not evidenced, True


def analyze_report(path: Path) -> TaskAgency | None:
    """分析一份 ``task_*.json`` 报告，返回它的 agency 行数据。

    报告里没有 ``session_trace`` 字段（旧报告，跑时没传 tree）→ 只填
    status/verdict/rounds 三个字段，trace 派生指标全部 ``None``。
    """
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    result = payload.get("result") or {}
    verdict = payload.get("verdict") or {}
    task_id = str(payload.get("task") or path.stem)
    profile = str(result.get("eval_profile") or "?")
    status = str(result.get("status") or "?")
    rounds = int(result.get("rounds") or 0)
    verdict_passed = (
        bool(verdict["passed"]) if isinstance(verdict.get("passed"), bool) else None
    )

    row = TaskAgency(
        task_id=task_id,
        eval_profile=profile,
        status=status,
        verdict_passed=verdict_passed,
        rounds=rounds,
        reason=(str(result["reason"]) if result.get("reason") else None),
    )

    # ---- trace 派生指标 ----
    trace_rel = payload.get("session_trace")
    if not trace_rel:
        return row  # 缺落盘：如实留 None
    events = _load_trace(REPO / str(trace_rel))
    if events is None:
        return row
    sequence = _tool_sequence(events)
    row = row.model_copy(
        update={
            "tool_sequence": sequence,
            "unverified_conclusion": _classify(sequence)[0],
            "self_continued": rounds >= 2,
        }
    )
    # M1/M2 依赖 status+verdict 两者都在（verdict 缺失 = 没判定，无定义）
    if verdict_passed is not None:
        if status == "completed":
            row = row.model_copy(
                update={
                    "converged_clean": verdict_passed,
                    "falsely_completed": not verdict_passed,
                }
            )
        elif verdict_passed is False:
            # 任务没做成：如实收尾 = stopped 且带 reason（"修完了但被掐"
            # 这类 stopped+PASS 不进这里——它 verdict 通过了）。
            row = row.model_copy(
                update={
                    "stall_self_report": status == "stopped"
                    and bool(row.reason and row.reason.strip()),
                }
            )
    return row


def _rate(numerator: list[TaskAgency], denominator: list[TaskAgency]) -> str:
    """点估计 + 样本量。分母为 0 = 无定义（不是 0%）。"""
    if not denominator:
        return "—（无定义）"
    return f"{len(numerator)}/{len(denominator)} = {len(numerator) / len(denominator):.0%}"


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return float(ordered[mid]) if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def build_report(rows: list[TaskAgency], stamp: str, commit: str) -> tuple[str, str]:
    """三档格式（对齐 aggregate.py）：逐任务表 + 汇总表 + 结论位。"""
    by_task: dict[str, list[TaskAgency]] = {}
    for row in rows:
        by_task.setdefault(row.task_id, []).append(row)

    completed = [r for r in rows if r.status == "completed"]
    converged_clean = [r for r in completed if r.converged_clean is True]
    falsely = [r for r in completed if r.falsely_completed is True]
    failed_verdict = [r for r in rows if r.verdict_passed is False]
    self_reported = [r for r in failed_verdict if r.stall_self_report is True]
    m3_denom = [r for r in rows if r.unverified_conclusion is not None]
    m3_num = [r for r in m3_denom if r.unverified_conclusion is True]
    continued = [r for r in rows if r.self_continued is True]
    completed_rounds = [r.rounds for r in completed]

    lines: list[str] = [
        "# agency 评测轴报告（阶段 0 基线）",
        "",
        f"> 生成时间 {stamp} ｜ 提交号 `{commit}` ｜ 样本 {len(rows)} 行"
        f"（{len(by_task)} 任务 × 各档位）",
        "> 口径：每任务 1 次采样，点估计 + 样本量；不给置信区间外结论。",
        "> 数据源：task report（status/verdict/rounds）+ TraceHook 落盘（tool 序列）。",
        "",
        "## 逐任务行数据",
        "",
        "| 任务 | 档位 | status | verdict | 轮数 | 收尾理由 | M3 未查证即结论 | M4 自发推进 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda r: (r.task_id, r.eval_profile)):
        verdict = "—" if row.verdict_passed is None else ("✓" if row.verdict_passed else "✗")
        m3 = "—" if row.unverified_conclusion is None else ("**是**" if row.unverified_conclusion else "否")
        m4 = "—" if row.self_continued is None else ("是" if row.self_continued else "否")
        reason = (row.reason or "")[:40].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {row.task_id} | {row.eval_profile} | `{row.status}` | {verdict} "
            f"| {row.rounds} | {reason} | {m3} | {m4} |"
        )

    lines += [
        "",
        "## 汇总（点估计，分母不同是因为各指标适用范围不同）",
        "",
        "| 指标 | 值 | 分母口径 |",
        "| --- | --- | --- |",
        f"| **M1 真收束率** | {_rate(converged_clean, completed)} "
        f"| status=completed 的任务 |",
        f"| **M1' 谎报完成率** | {_rate(falsely, completed)} "
        f"| status=completed 的任务 |",
        f"| **M2 卡点自陈率** | {_rate(self_reported, failed_verdict)} "
        f"| verdict 未过的任务 |",
        f"| **M3 未查证即结论率** | {_rate(m3_num, m3_denom)} "
        f"| 有 write/edit 调用的任务（bash 不计入） |",
        f"| **M4 后续步骤自发起率** | {_rate(continued, [r for r in rows if r.self_continued is not None])} "
        f"| trace 可算的任务 |",
        "",
        f"- 收束轮数中位数（completed）：{_median(completed_rounds) if completed_rounds else '—'}",
        "",
        "## 口径边界（引用前必读）",
        "",
        "1. **N=1 采样**。temperature=0 但服务端非确定——syn-004 同档重跑曾有",
        "   8 轮 vs 16 轮差异。本报告是方向性数字，不是统计量。",
        "2. **M3 的查证类不含 bash**（读写混合无法静态区分），故 M3 是未查证率的",
        "   **上界**；任务无写类调用时不进分母（无「结论」动作，指标无定义）。",
        "3. **缺 trace 的行指标留 None**（跑时没传 ``--sessions-dir``），不静默变 0。",
        "4. B0（无工具单轮）上 M3/M4 无定义——它没有工具调用。agency 对照臂是",
        "   **B1**（开工指令 §4.2 硬提醒）。",
        "",
    ]
    return "\n".join(lines) + "\n", json.dumps(
        {
            "generated_at": stamp,
            "commit": commit,
            "rows": [r.model_dump() for r in rows],
        },
        ensure_ascii=False,
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从既有落盘数据算 agency 评测轴")
    parser.add_argument(
        "--profile",
        default=None,
        help="只要这个档位（如 B1）。缺省 = 全部档位",
    )
    parser.add_argument(
        "--tasks",
        default=None,
        help="逗号分隔的任务 id 过滤（如 ax101,ax102）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="报告输出路径（默认 evals/reports/agency_<stamp>.md）",
    )
    args = parser.parse_args(argv)

    wanted = set(args.tasks.split(",")) if args.tasks else None
    rows: list[TaskAgency] = []
    for path in sorted(REPORTS.glob("task_*.json")):
        # 文件名 = task_<任务 id>_<档位>.json；任务 id 自带连字符（ax101-impl-roman），
        # 所以按 "_" 切最后一节当档位、其余还原成任务 id——不能拿任务 id 的
        # 前缀去拼文件名（"ax101" 拼不出 "task_ax101_"）。
        stem = path.stem[len("task_") :]
        task_id, _, _profile = stem.rpartition("_")
        if wanted is not None and task_id not in wanted:
            continue
        row = analyze_report(path)
        if row is None:
            continue
        if args.profile is not None and row.eval_profile != args.profile:
            continue
        rows.append(row)

    if not rows:
        print(
            "[错误] 没有可分析的报告。先跑 task_runner 并传 --sessions-dir"
            "（trace 落盘是 M3/M4 的数据源）。",
            file=sys.stderr,
        )
        return 2

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    commit = _git_commit()
    md, payload = build_report(rows, stamp, commit)

    md_path = Path(args.out) if args.out else REPORTS / f"agency_{stamp.replace(':', '').replace('-', '')}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md, "utf-8")
    json_path = md_path.with_suffix(".json")
    json_path.write_text(payload, "utf-8")
    print(f"agency 轴：{len(rows)} 行 → {md_path.resolve().relative_to(REPO.resolve())} / {json_path.resolve().relative_to(REPO.resolve())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
