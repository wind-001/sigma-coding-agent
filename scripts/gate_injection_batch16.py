"""P5-批次1 门槛注入实验：逐条证伪 G865–G870、G872（观测层）。

为什么必须做这个
    P0 固化的核心教训：**「配置跑绿」不等于「约束生效」**。
    观测层尤其危险——它的失败模式全是静默的：少一行 trace、
    延迟取错点、近似值冒充精确值，任务照跑、测试照绿。
    所以每条注入都要同时检查**基线绿**与**注入后红**。

**为什么在真实仓库上做**
    批次 1 踩过的坑：`.venv` 里 `pip install -e .` 生成的 `.pth`
    把真实仓库的绝对路径写死，"复制到临时目录再注入"是假实验。
    正确做法是在真实仓库上改、跑、还原，**还原放 `finally`**。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch16.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

print(f"[env] 仓库根 = {REPO}")
print(f"[env] python  = {PYTHON}")
assert (REPO / "core" / "sigma_session" / "trace.py").exists(), "仓库根解析错了"


class Repo:
    """在真实仓库上做受控破坏，并保证还原。"""

    def __init__(self) -> None:
        self._pending: list[tuple[Path, str]] = []  # (文件, 原始内容)

    def patch(self, rel_path: str, old: str, new: str) -> None:
        """替换，并登记原始内容以便还原。

        锚点不中就直接抛错——静默不匹配会让注入实验假绿，
        那正是本脚本要防的那类问题。
        """
        path = REPO / rel_path
        text = path.read_text(encoding="utf-8")

        if not any(p == path for p, _ in self._pending):
            self._pending.append((path, text))

        if old not in text:
            raise AssertionError(f"{rel_path}: 注入锚点没找到：{old[:70]!r}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def restore(self) -> None:
        for path, original in reversed(self._pending):
            path.write_text(original, encoding="utf-8")
        self._pending.clear()

    def run_pytest(self, target: str) -> tuple[int, str]:
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


RESULTS: list[tuple[str, str, bool, str]] = []


def experiment(gate: str, what: str, target: str, inject) -> None:
    """跑一次注入实验：基线绿 → 注入 → 必须红 → 还原。"""
    repo = Repo()
    try:
        code, out = repo.run_pytest(target)
        if code != 0:
            RESULTS.append(
                (gate, what, False, f"基线不是绿的，实验无效：{out.strip()[-200:]}")
            )
            return
        inject(repo)
        code, out = repo.run_pytest(target)
        if code == 0:
            RESULTS.append(
                (gate, what, False, "注入后**仍然全绿** → 门槛没有在防它声称防的东西")
            )
            return
        failing = [
            line
            for line in out.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR") or "BROKEN" in line
        ]
        detail = failing[0][:150] if failing else out.strip().splitlines()[-1][:150]
        RESULTS.append((gate, what, True, detail))
    finally:
        repo.restore()


# ---------------------------------------------------------------------------
# 各门槛的注入
# ---------------------------------------------------------------------------

TRACE = "core/sigma_session/trace.py"
LOOP = "core/sigma_agent/loop.py"
TIMELINE = "core/sigma/timeline.py"
TRACE_TESTS = "tests/test_trace_hook.py"
TIMELINE_TESTS = "tests/test_timeline_view.py"


def _inject_e865(repo: Repo) -> None:
    """E865 / G865：TraceHook 取消全部订阅——事件流照发，trace 零行。"""
    repo.patch(
        TRACE,
        "    def events(self) -> tuple[type[HookEvent], ...]:\n        return (\n            LlmRequested,",
        "    def events(self) -> tuple[type[HookEvent], ...]:\n"
        "        return ()  # 注入：静默退订\n"
        "        return (\n            LlmRequested,",
    )


def _inject_e866(repo: Repo) -> None:
    """E866 / G866：删掉 LlmRequested 的 emit——延迟与 TTFT 全部失测。"""
    repo.patch(
        LOOP,
        "            await self._emit(LlmRequested())",
        "            pass  # 注入：移除请求锚点",
    )


def _inject_e867(repo: Repo) -> None:
    """E867 / G867：trace 失败穿透（自宽容失效）——写入失败会炸掉任务。"""
    repo.patch(
        TRACE,
        "            self._dead = True",
        "            raise  # 注入：失败穿透，任务陪葬",
    )


def _inject_e868(repo: Repo) -> None:
    """E868 / G868：删掉 ApprovalDecided 的 emit——审批决策重新零留痕。"""
    repo.patch(
        LOOP,
        "                if self._hooks.approval_names():\n"
        "                    await self._emit(\n"
        "                        ApprovalDecided(name=plan.tool.name, decision=decision)\n"
        "                    )",
        "                if self._hooks.approval_names():\n"
        "                    pass  # 注入：审批不留痕",
    )


def _inject_e869(repo: Repo) -> None:
    """E869 / G869：token 汇总改成"装最后一轮"——复刻 2026-09-21 那个真 bug。"""
    repo.patch(
        TIMELINE,
        "            total_prompt += prompt\n"
        "            total_completion += completion\n"
        "            total_cached += cached",
        "            total_prompt = prompt  # 注入：只装最后一轮\n"
        "            total_completion = completion\n"
        "            total_cached = cached",
    )


def _inject_e870(repo: Repo) -> None:
    """E870 / G870：忽略 trace 精确延迟——近似值冒充观测结果。"""
    repo.patch(
        TIMELINE,
        "                    if isinstance(latency, (int, float)):\n"
        "                        slot[\"latency\"] = round(float(latency))\n"
        "                        slot[\"approx\"] = False",
        "                    if isinstance(latency, (int, float)):\n"
        "                        pass  # 注入：丢弃 trace 精确值",
    )


def _inject_e871(repo: Repo) -> None:
    """E871 / G871：trace_path_for 返回**会话文件本身**——观测写进审计链。

    trace 的每一行都会落进会话 JSONL：重载后 clean=False、节点数变多，
    G871 的落盘重载比对当场红。
    """
    repo.patch(
        TRACE,
        "    jsonl = JsonlStore(Path(sessions_dir), session_id).path\n"
        "    return jsonl.with_name(jsonl.stem + \".trace.jsonl\")",
        "    return JsonlStore(Path(sessions_dir), session_id).path  # 注入：写进会话文件",
    )


def _inject_e872(repo: Repo) -> None:
    """E872 / G872：timer 恒用真实时钟——注入的确定性时钟被绕开。"""
    repo.patch(
        TRACE,
        "        self._timer = timer or time.monotonic",
        "        self._timer = time.monotonic  # 注入：无视注入钟",
    )


EXPERIMENTS = [
    ("E865", "G865  TraceHook 静默退订 → trace 零行", f"{TRACE_TESTS}::test_g865_trace_lines_follow_event_order", _inject_e865),
    ("E866", "G866  删 LlmRequested emit → 延迟失测", f"{TRACE_TESTS}::test_g866_latency_equals_injected_timer_deltas", _inject_e866),
    ("E867", "G867  trace 失败穿透 → 任务陪葬", f"{TRACE_TESTS}::test_g867_write_failure_is_self_tolerant", _inject_e867),
    ("E868", "G868  删审批留痕 emit → 决策零记录", f"{TRACE_TESTS}::test_g868_approval_decisions_are_recorded", _inject_e868),
    ("E869", "G869  token 汇总只装最后一轮（历史真 bug）", f"{TIMELINE_TESTS}::test_g869_legacy_session_renders_from_jsonl_alone", _inject_e869),
    ("E870", "G870  忽略 trace 精确值 → 近似冒充", f"{TIMELINE_TESTS}::test_g870_trace_values_win_over_approximation", _inject_e870),
    ("E871", "G871  观测写进会话文件 → 审计链污染", f"{TRACE_TESTS}::test_g871_observation_pollutes_nothing", _inject_e871),
    ("E872", "G872  timer 绕开注入钟 → 确定性破坏", f"{TRACE_TESTS}::test_g872_trace_is_deterministic", _inject_e872),
]


def main() -> int:
    for gate, what, target, inject in EXPERIMENTS:
        print(f"[{gate}] 注入：{what}")
        experiment(gate, what, target, inject)

    print()
    print("=" * 78)
    print("P5-批次1 门槛注入实验结果")
    print("=" * 78)
    ok = 0
    for gate, what, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        if passed:
            ok += 1
        print(f"[{mark}] {gate}  {what}")
        print(f"       → {detail}")
    print("=" * 78)
    print(f"{ok}/{len(RESULTS)} 条门槛被成功证伪（注入后确实变红）")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
