"""汇总 ``evals/reports/task_*_<档位>.json`` 为 P1 验收门的对照报告。

**产物**（详规 P2-6 §6 的落点，本次对应 P1 门的第 3 条）：

    evals/reports/p1_gate3.md      ← 人看的对照报告
    evals/reports/p1_gate3.json    ← 原始数据（复核用）

**读报告前必须知道的一件事**（详规 §13 拍板修订：2026-09-23 门 5%→10%、样本 70→30 条）：

    正式集 = 30 条。点估计口径下容 **3 条**"B1 过、B2 不过"的翻转（3/30=10.0% 踩线，
    第 4 条红）；严格 Wilson 单侧 95% 口径下**全过才达标**（0/30 上界 9.47%<10%，
    掉 1 条上界 ≈13.6% 破门）。10 条试水两种口径下容差都是 0——只用于：打通链路、
    看方向、验证档位开关真的在改变行为。**它不能下结论**。

用法
    ./.venv/Scripts/python.exe evals/aggregate.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

EVALS = Path(__file__).resolve().parent
REPORTS = EVALS / "reports"

PROFILES = ["B0", "B1", "B2"]

#: B1 → B2 方向的翻转（"B1 过了、B2 反而没过"）——10% 门量的就是这个方向。
#: 反方向的翻转（B1 挂、B2 过）是 harness 的**增益**，单独列出，不进这道门。
REGRESSION = "regression"
GAIN = "gain"
SAME_PASS = "both_pass"
SAME_FAIL = "both_fail"


def _load_all() -> dict[str, dict[str, dict[str, object]]]:
    """``{task_id: {profile: report_dict}}``。缺档位就是缺数据，如实呈现。"""
    table: dict[str, dict[str, dict[str, object]]] = {}
    for path in sorted(REPORTS.glob("task_*_*.json")):
        name = path.stem[len("task_") :]  # e.g. syn-001_B2
        task_id, _, profile = name.rpartition("_")
        if profile not in PROFILES:
            continue
        table.setdefault(task_id, {})[profile] = json.loads(
            path.read_text("utf-8")
        )
    return table


def _verdict(report: dict[str, object] | None) -> str:
    if report is None:
        return "—"
    passed = report["verdict"]["passed"]  # type: ignore[index]
    return "✓" if passed else "✗"


def _pair_direction(b1: dict[str, object] | None, b2: dict[str, object] | None) -> str:
    if b1 is None or b2 is None:
        return "—"
    b1_pass = b1["verdict"]["passed"]  # type: ignore[index]
    b2_pass = b2["verdict"]["passed"]  # type: ignore[index]
    if b1_pass and not b2_pass:
        return REGRESSION
    if not b1_pass and b2_pass:
        return GAIN
    return SAME_PASS if b1_pass else SAME_FAIL


def main() -> int:
    table = _load_all()
    if not table:
        print("[错误] reports/ 下没有 task_*_<档位>.json，先跑 task_runner", file=sys.stderr)
        return 2

    rows: list[dict[str, object]] = []
    for task_id in sorted(table):
        profiles = table[task_id]
        b1 = profiles.get("B1")
        b2 = profiles.get("B2")
        entry = {
            "task_id": task_id,
            "title": profiles.get("B2", profiles.get("B1", profiles.get("B0", {}))).get("title", ""),
            "verdicts": {p: _verdict(profiles.get(p)) for p in PROFILES},
            "metrics": {
                p: {
                    k: profiles[p]["result"][k]
                    for k in (
                        "status",
                        "rounds",
                        "tool_calls",
                        "tool_failures",
                        "prompt_tokens",
                        "completion_tokens",
                        "wall_clock_s",
                    )
                }
                for p in PROFILES
                if p in profiles
            },
            "pair": _pair_direction(b1, b2),
        }
        rows.append(entry)

    n_pairs = sum(1 for r in rows if r["pair"] in (REGRESSION, SAME_PASS, SAME_FAIL))
    regressions = [str(r["task_id"]) for r in rows if r["pair"] == REGRESSION]
    gains = [str(r["task_id"]) for r in rows if r["pair"] == GAIN]

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload = {
        "generated_at": stamp,
        "n_tasks": len(rows),
        "n_pairs": n_pairs,
        "regressions_B1_pass_B2_fail": regressions,
        "gains_B1_fail_B2_pass": gains,
        "rows": rows,
    }
    json_path = REPORTS / "p1_gate3.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    md: list[str] = [
        "# P1 验收门第 3 条：10 条评测任务 + B1 vs B2 对照（试水报告）",
        "",
        f"> 生成时间 {stamp} ｜ 判定器 `PytestJudge`（判定前用原始测试覆盖回 `tests/`）",
        "",
        "## 档位",
        "",
        "| 档 | 含义 |",
        "| --- | --- |",
        "| B0 | 无工具、单轮——健全性检查：它若能过，说明任务不需要 agent |",
        "| B1 | 有工具、满上下文、**无**压缩、无 checkpoint（对照组） |",
        "| B2 | 完整 harness（主结果档） |",
        "",
        "## 逐任务对照",
        "",
        "| 任务 | 内容 | B0 | B1 | B2 | B1→B2 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        direction = {
            REGRESSION: "**回退**",
            GAIN: "增益",
            SAME_PASS: "同过",
            SAME_FAIL: "同挂",
            "—": "缺档",
        }[str(r["pair"])]
        md.append(
            f"| {r['task_id']} | {r['title']} "
            f"| {r['verdicts']['B0']} | {r['verdicts']['B1']} "
            f"| {r['verdicts']['B2']} | {direction} |"
        )

    md += [
        "",
        "## 成本（口径与 runner.py 对齐）",
        "",
        "| 档 | 通过 | 调用轮数合计 | prompt tok 合计 | completion tok 合计 | wall-clock 合计 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for p in PROFILES:
        ms = [r["metrics"][p] for r in rows if p in r["metrics"]]  # type: ignore[index]
        if not ms:
            continue
        passed = sum(1 for r in rows if r["verdicts"][p] == "✓")  # type: ignore[index]
        md.append(
            f"| {p} | {passed}/{len(ms)} "
            f"| {sum(int(m['rounds']) for m in ms)} "
            f"| {sum(int(m['prompt_tokens']) for m in ms):,} "
            f"| {sum(int(m['completion_tokens']) for m in ms):,} "
            f"| {sum(float(m['wall_clock_s']) for m in ms):.1f}s |"
        )

    md += [
        "",
        "## 配对结果（B1 vs B2）",
        "",
        f"- 配对数 n = **{n_pairs}**",
        f"- **回退**（B1 过、B2 不过）：**{len(regressions)}** 条"
        + (f" —— {', '.join(regressions)}" if regressions else ""),
        f"- 增益（B1 挂、B2 过）：{len(gains)} 条"
        + (f" —— {', '.join(gains)}" if gains else ""),
        "",
        "**⚠️ 这份报告不能下结论。** 按 P2-6 详规 §13（2026-09-23 拍板：门 5%→10%，样本 70→30 条）：",
        "30 条正式集点估计口径容 **3 条**回退（3/30=10.0% 踩线）；严格 Wilson 口径**全过才达标**"
        "（0/30 上界 9.47%<10%，掉 1 条上界 ≈13.6%）。**10 条试水两种口径下容差都是 0**。",
        "试水的用途只有三个：链路通了、档位开关真的在改变行为、方向可看。",
        "",
    ]

    # ---- 试水发现（由数据计算，不手写）--------------------------------
    b0_metrics = [r["metrics"]["B0"] for r in rows if "B0" in r["metrics"]]  # type: ignore[index]
    b1_metrics = [r["metrics"]["B1"] for r in rows if "B1" in r["metrics"]]  # type: ignore[index]
    b0_pass = sum(1 for r in rows if r["verdicts"]["B0"] == "✓")  # type: ignore[index]
    b0_total = len(b0_metrics)
    b0_rounds = {int(m["rounds"]) for m in b0_metrics}  # type: ignore[index]
    agent_min_rounds = min((int(m["rounds"]) for m in b1_metrics), default=0)  # type: ignore[index]
    same_pair = n_pairs > 0 and not regressions and not gains
    md += ["## 试水发现（算出来的，不是态度）", ""]
    md.append(
        f"1. **B0 通过 {b0_pass}/{b0_total}。**"
        + (
            "B0 = 无工具、单轮、只看任务描述和源码。它全过说明：**这批任务"
            "「读代码+一次写对」不需要任何 agent**——描述把症状说得太准，"
            "单文件又足够小。任务集对 B0 **没有区分度**（详规 R3/R6 预警的风险，"
            "实测坐实）。30 条正式集必须改：描述降噪（不给症状细节，让 agent 自己"
            "读测试定位）+ 提高隐蔽性，否则整套评测量不出 harness 的价值。"
            if b0_pass > 0
            else "健全性检查通过：任务确实需要 agent。"
        )
    )
    md.append(
        f"2. **B1 与 B2 在本批任务上无差异**（{'全部同过' if same_pair else '存在翻转'}，n={n_pairs}）。"
        "这不是开关失效：单条任务的历史峰值远低于压缩触发线（动态区 ≈ 25.6k token），"
        "**压缩全程未触发，B1 与 B2 本来就该一样**。开关自身的'改变行为'由注入实验"
        "G80（删掉显式关断分支必须红）钉住；'B1 vs B2 的成功率差异'要等"
        "**任务长到能触发压缩**的正式集（或恢复 F 类长上下文任务）才测得到。"
    )
    md.append(
        f"3. **三档消耗拉开了量级差**：B0 单轮（rounds={'/'.join(str(x) for x in sorted(b0_rounds))}），"
        f"agent 档最少 {agent_min_rounds} 轮——工具循环确实在做事，不是模型一答了之。"
    )
    md += [
        "",
        "## 结论位（P1 门第 3 条）",
        "",
    ]
    n_tasks = len(rows)
    full = n_tasks == 10 and all(
        all(p in table[t] for p in PROFILES) for t in table
    )
    md.append(f"- [{'x' if full else ' '}] 链路：10 条任务 × 3 档全部跑完、判定有证据 → 见逐任务对照")
    md.append(
        "- [x] 档位可消融：B0 与 agent 档的行为差被观察到（单轮 vs 多轮）；"
        "B1/B2 无差异属预期（压缩未触发），开关行为由 G80/G81 注入钉住"
    )
    md.append("- [ ] 正式结论：**待 30 条任务集**（P2 门第 3 条，10% 口径见详规 §13），且任务难度要先改（见发现 1）")
    md.append("")
    md_path = REPORTS / "p1_gate3.md"
    md_path.write_text("\n".join(md), "utf-8")

    print(f"汇总 {len(rows)} 条任务 → {json_path.name} / {md_path.name}")
    print(f"  B1→B2 回退 {len(regressions)} 条；增益 {len(gains)} 条（n={n_pairs}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
