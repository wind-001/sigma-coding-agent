"""P5-批次2 波 1 门槛注入实验:逐条证伪 G873/G874/G875/G878(自证矩阵)。

为什么必须做这个
    P0 固化的教训:**「配置跑绿」不等于「约束生效」**。自证报告尤其
    危险——它的失败模式是"报告行看起来有数字,实际口径错了":
    汇总只装最后一条、比对只查文件名、拦截率把放行也数进去、
    steering 根本没送达但报告照绿。每条注入都验证"基线绿→注入红"。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch17.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

print(f"[env] 仓库根 = {REPO}")
print(f"[env] python  = {PYTHON}")
assert (REPO / "evals" / "self_check.py").exists(), "仓库根解析错了"


class Repo:
    def __init__(self) -> None:
        self._pending: list[tuple[Path, str]] = []

    def patch(self, rel_path: str, old: str, new: str) -> None:
        path = REPO / rel_path
        text = path.read_text(encoding="utf-8")
        if not any(p == path for p, _ in self._pending):
            self._pending.append((path, text))
        if old not in text:
            raise AssertionError(f"{rel_path}: 注入锚点没找到:{old[:70]!r}")
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
    repo = Repo()
    try:
        code, out = repo.run_pytest(target)
        if code != 0:
            RESULTS.append((gate, what, False, f"基线不是绿的,实验无效:{out.strip()[-200:]}"))
            return
        inject(repo)
        code, out = repo.run_pytest(target)
        if code == 0:
            RESULTS.append((gate, what, False, "注入后**仍然全绿** → 门槛没有在防它声称防的东西"))
            return
        failing = [
            line for line in out.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR") or "BROKEN" in line
        ]
        detail = failing[0][:150] if failing else out.strip().splitlines()[-1][:150]
        RESULTS.append((gate, what, True, detail))
    finally:
        repo.restore()


SELF_CHECK = "evals/self_check.py"
ROLLBACK = "evals/rollback_stats.py"
APPROVAL = "core/sigma/_approval.py"
TESTS = "tests/test_self_check.py"


def _inject_e873(repo: Repo) -> None:
    """E873 / G873:汇总改成"只装最后一个会话"——复刻 2026-09-21 真 bug。"""
    repo.patch(
        SELF_CHECK,
        "        stats.total_prompt += row.prompt_tokens\n"
        "        stats.total_cached += row.cached_tokens",
        "        stats.total_prompt = row.prompt_tokens  # 注入:只装最后一个\n"
        "        stats.total_cached = row.cached_tokens",
    )


def _inject_e874(repo: Repo) -> None:
    """E874 / G874:比对退化成只查文件名集合——同名不同内容静默放过。"""
    repo.patch(
        ROLLBACK,
        '        elif actual[name] != expected[name]:\n            diffs.append(f"内容不一致 {name}")',
        '        elif False:  # 注入:内容不比对\n            diffs.append(f"内容不一致 {name}")',
    )


def _inject_e875(repo: Repo) -> None:
    """E875 / G875:审批闸改全放行——拦截率统计必须当场塌掉。"""
    repo.patch(
        APPROVAL,
        "        findings = analyze_call(name, arguments, self._workspace)\n        if not findings:\n            return ApprovalDecision(allowed=True)",
        "        return ApprovalDecision(allowed=True)  # 注入:闸失效,全放行\n"
        "        findings = analyze_call(name, arguments, self._workspace)\n        if not findings:\n            return ApprovalDecision(allowed=True)",
    )


def _inject_e878(repo: Repo) -> None:
    """E878 / G878:steering 半路丢失——报告若照绿,送达断言就是假的。"""
    repo.patch(
        SELF_CHECK,
        "        text = pending.pop(0)\n        return [",
        "        pending.clear()  # 注入:steering 丢失\n        return [",
    )


EXPERIMENTS = [
    ("E873", "G873  缓存汇总只装最后一个会话(历史真 bug 形状)", f"{TESTS}::test_g873_cache_stats_accumulates_all_sessions", _inject_e873),
    ("E874", "G874  回滚比对退化为只查文件名", f"{TESTS}::test_g874_tree_diff_is_content_sensitive", _inject_e874),
    ("E875", "G875  审批闸全放行 → 拦截率塌掉", f"{TESTS}::test_g875_gate_replay_full_dataset", _inject_e875),
    ("E878", "G878  steering 半路丢失", f"{TESTS}::test_g878_steering_delivered_and_harmless", _inject_e878),
]


def main() -> int:
    for gate, what, target, inject in EXPERIMENTS:
        print(f"[{gate}] 注入:{what}")
        experiment(gate, what, target, inject)

    print()
    print("=" * 78)
    print("P5-批次2 波 1 门槛注入实验结果")
    print("=" * 78)
    ok = 0
    for gate, what, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        if passed:
            ok += 1
        print(f"[{mark}] {gate}  {what}")
        print(f"       → {detail}")
    print("=" * 78)
    print(f"{ok}/{len(RESULTS)} 条门槛被成功证伪(注入后确实变红)")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
