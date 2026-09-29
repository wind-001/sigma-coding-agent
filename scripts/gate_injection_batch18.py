"""P5-批次3 门槛注入实验:逐条证伪 G879–G884(预算修订+跨会话记忆)。

为什么必须做这个
    P0 固化的教训:**「配置跑绿」不等于「约束生效」**。本批两件高危:
    ①预算修订(总额动了两格)——注入"分项和≠总额仍绿"证明 G885 在防;
    ②记忆注入常驻区——注入"空工作区也注入/超预算不抛/写后重扫"分别
    证明 G881/G883/G884 在防。每条都要基线绿→注入红。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch18.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

print(f"[env] 仓库根 = {REPO}")
print(f"[env] python  = {PYTHON}")
assert (REPO / "core" / "sigma_session" / "memory.py").exists(), "仓库根解析错了"


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


MEMORY = "core/sigma_session/memory.py"
CONTEXT = "core/sigma_session/context.py"
CHECKPOINT = "core/sigma_agent/checkpoint.py"
SDK = "core/sigma/sdk.py"
MEM_TESTS = "tests/test_memory.py"
BUDGET_TESTS = "tests/test_resident_budget.py"


def _inject_e879(repo: Repo) -> None:
    """E879 / G879:扫描器 glob 置空——写了记忆也永远发现不了。"""
    repo.patch(
        MEMORY,
        '    for path in sorted(memory_dir.glob("*.md"), key=lambda p: p.name):',
        '    for path in sorted(memory_dir.glob("*.nonexistent"), key=lambda p: p.name):  # 注入:发现失效',
    )


def _inject_e880(repo: Repo) -> None:
    """E880 / G880:索引 cap 失效——记忆多了把常驻区撑爆且无人知道。"""
    repo.patch(
        MEMORY,
        "        limit = MEMORY_INDEX_CAP_TOKENS - (reserve if shown + 1 < len(scan.entries) else 0)",
        "        limit = 10 ** 9  # 注入:cap 失效",
    )


def _inject_e881(repo: Repo) -> None:
    """E881 / G881:空工作区也注入——'零污染'承诺失效。"""
    repo.patch(
        MEMORY,
        '    if not scan.entries:\n        return ""',
        '    if not scan.entries:\n        return "跨会话记忆索引（空）"  # 注入:空也注入',
    )


def _inject_e882(repo: Repo) -> None:
    """E882 / G882:checkpoint 排除清单丢掉 .sigma/——回滚会删掉记忆。"""
    repo.patch(
        CHECKPOINT,
        '    ".git/",\n    ".sigma/",',
        '    ".git/",  # 注入:.sigma 移出排除清单',
    )


def _inject_e883(repo: Repo) -> None:
    """E883 / G883:memory_index 不计预算——超限静默(变贵不报错)。"""
    repo.patch(
        CONTEXT,
        "        return estimate_text(self._resident_text()) + estimate_text(\n            self._tools_schema_text()\n        )",
        "        return estimate_text(self._resident_text()) + estimate_text(\n            self._tools_schema_text()\n        ) - estimate_text(self._memory_index)  # 注入:记忆豁免预算",
    )


def _inject_e884(repo: Repo) -> None:
    """E884 / G884:send 路径重扫记忆——常驻区会话内变化,缓存失效。"""
    repo.patch(
        SDK,
        "        self._memory_index = (\n            render_memory_index(self._memory_scan)\n            if self._memory_scan is not None\n            else \"\"\n        )",
        "        self._memory_index = (\n            render_memory_index(self._memory_scan)\n            if self._memory_scan is not None\n            else \"\"\n        )\n        self._rescan_on_send = True  # 注入:暴露重扫开关",
    )
    repo.patch(
        SDK,
        "    async def send(self, task: str) -> TurnResult:",
        "    async def send(self, task: str) -> TurnResult:\n"
        "        if getattr(self, '_rescan_on_send', False) and self._enable_memory:\n"
        "            self._memory_scan = scan_memory(memory_dir_for(self._workspace_root))  # 注入:重扫\n"
        "            self._context._memory_index = render_memory_index(self._memory_scan)\n"
        "            self._context._fingerprint = self._context._compute_fingerprint()",
    )


EXPERIMENTS = [
    ("E879", "G879  扫描器发现失效 → 写了也看不见", f"{MEM_TESTS}::test_g879_cross_session_memory_loop", _inject_e879),
    ("E880", "G880  索引 cap 失效 → 常驻区静默膨胀", f"{MEM_TESTS}::test_g880_index_capped_and_truncation_visible", _inject_e880),
    ("E881", "G881  空工作区也注入 → 零污染承诺失效", f"{MEM_TESTS}::test_g881_empty_workspace_zero_pollution", _inject_e881),
    ("E882", "G882  排除清单丢 .sigma/ → 回滚删记忆", f"{MEM_TESTS}::test_g882_memory_survives_rollback", _inject_e882),
    ("E883", "G883  记忆豁免预算 → 超限静默", f"{MEM_TESTS}::test_g883_memory_index_counts_toward_budget", _inject_e883),
    ("E884", "G884  send 路径重扫 → 缓存失效", f"{MEM_TESTS}::test_g884_scan_happens_exactly_once", _inject_e884),
    ("E885", "G885  改分项不 sum 校验 → 表自相矛盾", f"{BUDGET_TESTS}::test_g885_caps_sum_equals_budget", None),
]


def _e885(repo: Repo) -> None:
    repo.patch(
        "core/sigma_session/resident_caps.py",
        '    "具名预留(repo map 等)": 500,',
        '    "具名预留(repo map 等)": 900,  # 注入:分项和==5540≠总额',
    )


EXPERIMENTS[-1] = ("E885", "G885  改分项使和≠总额 → 表自相矛盾", f"{BUDGET_TESTS}::test_g885_caps_sum_equals_budget", _e885)


def main() -> int:
    for gate, what, target, inject in EXPERIMENTS:
        print(f"[{gate}] 注入:{what}")
        experiment(gate, what, target, inject)

    print()
    print("=" * 78)
    print("P5-批次3 门槛注入实验结果")
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
