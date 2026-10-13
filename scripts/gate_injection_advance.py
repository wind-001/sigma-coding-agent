"""推进机制批次 1+2+3+4 门槛注入实验：GA-4 证伪扩展面零值承诺、GA-1 证伪验证收尾、GA-2 证伪清单延续、GA-3 证伪正向推进注入。

为什么必须做这个（P0 核心教训，已固化为验收门）
    「配置跑绿」不等于「约束生效」。一条门槛如果从来没有红过，就不知道
    它到底在不在工作。本脚本对每条承诺各做一次**注入实验**：故意把被防的
    问题造出来，确认对应断言真的红，然后还原、确认真的绿。

承诺与各自的注入靶
    GA-4a  零推进钩子 → advance_decide 返回 None
           注入：把 "return None" 改成恒返回 continue 决定
           （腐化形状：钩子面在，但"没人注册"这件事被抹掉——
           评测/回放/子会话不再静默，全部被强行续命）

    GA-4b  loop 侧只在有总线时才问（hooks=None 的旧路径不被碰）
           注入：把 "if self._hooks is not None:" 改成 "if True:"
           （腐化形状：直接构造 AgentLoop 的旧测试/回放路径被拖去问一个
           不存在的总线 → AttributeError）

    GA-1a  验证收尾真的在跑命令（不过 → 续跑/停止）
           注入：把 "passed, output = await self._run_command()" 改成
           "passed, output = True, ''"（腐化形状：命令形同虚设，
           模型一收手就放行——ax107 型谎报完成复发）

    GA-1b  连续不过 → force_stop（不永远续命）
           注入：把 force_stop 决定改成 continue_round
           （腐化形状：死循环烧 token，且永远不 stopped）

    GA-2   清单延续真的在看清单（有 pending → 继续）
           注入：把 "if pending == 0" 的提前返回改成恒不干预
           （腐化形状：判据形同虚设——建了清单也永远放行，
           机制退化成名义开关）

    GA-3   正向推进注入真的在注入（连续 N 轮无 read/grep/task → 注入）
           注入：把 interval 判断改成恒返回 []（腐化形状：参数、计数、
           文案全在，但一次都不注入——机制退化成名义开关，
           症状 B"只刹车不推进"复发）

    GA-3b  注入文案与注册表同源（没有 task 工具就不提"派子 agent"）
           注入：把 dispatch 的条件拼接改成恒带提示
           （腐化形状：模型按提示去调一个不存在的工具，白花一轮预算）

**为什么在真实仓库上做，而不是复制到临时目录**（batch1 的教训）：
    .venv 里 pip install -e 的 .pth 把真实仓库绝对路径写死了，临时目录里
    跑 pytest 导入到的仍是真实仓库的代码——沙箱改动不参与测试，
    7 条注入实验全部假绿。正确做法：真实仓库上改、跑、还原，
    **还原必须放在 finally 里**。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_advance.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

#: 本批次注入实验的目标测试文件（基线必须绿、注入必须红、还原必须绿）。
TARGETS = ("tests/test_advance_hook.py", "tests/test_advance_gate.py", "tests/test_advance_goal.py")

#: 批次 4（正向推进注入）的证伪目标——只有它测注入机制本体与文案同源。
POSITIVE_TARGETS = ("tests/test_positive_steer.py",)


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
        """还原全部改动。失败也必须执行。"""
        for path, original in reversed(self._pending):
            path.write_text(original, encoding="utf-8")
        self._pending.clear()

    def run(self, targets: tuple[str, ...]) -> tuple[int, str]:
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


RESULTS: list[tuple[str, str, bool, str]] = []


def experiment(gate: str, what: str, inject, targets: tuple[str, ...]) -> None:
    repo = Repo()
    try:
        # 1. 基线必须绿——否则"注入后变红"说明不了任何事
        code, out = repo.run(targets)
        if code != 0:
            RESULTS.append(
                (gate, what, False, f"基线不是绿的，实验无效：{out.strip()[-200:]}")
            )
            return

        # 2. 注入
        inject(repo)

        # 3. 必须变红
        code, out = repo.run(targets)
        if code == 0:
            RESULTS.append(
                (
                    gate,
                    what,
                    False,
                    "注入后**仍然全绿** → 门槛没有在防它声称防的东西",
                )
            )
            return

        # 4. 记录"红在哪"，作为门槛确实起作用的证据
        failing = [
            line
            for line in out.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR")
        ]
        RESULTS.append((gate, what, True, "; ".join(failing[:3]) or out.strip()[-200:]))
    finally:
        repo.restore()
        # 5. 还原后必须复绿（还原没还原干净，比不实验更糟）
        code, out = repo.run(targets)
        if code != 0:
            RESULTS.append((gate, "还原后复绿", False, f"还原后仍红：{out.strip()[-200:]}"))
        else:
            RESULTS.append((gate, "还原后复绿", True, ""))


def _ga4a(repo: Repo) -> None:
    repo.patch(
        "src/sigma/hooks/base.py",
        """        if not self._advance_hooks:
            return None""",
        """        if not self._advance_hooks:
            return AdvanceDecision(continue_round=True, reason="腐化：零钩子也继续")""",
    )


def _ga4b(repo: Repo) -> None:
    repo.patch(
        "src/sigma/runtime/event_loop.py",
        """                if self._hooks is not None:
                    if guard is not None:
                        guard.pause()""",
        """                if True:
                    if guard is not None:
                        guard.pause()""",
    )


def _ga1a(repo: Repo) -> None:
    repo.patch(
        "src/sigma/runtime/advance.py",
        """        self._attempts += 1
        passed, output = await self._run_command()""",
        """        self._attempts += 1
        passed, output = True, \"\"""",
    )


def _ga1b(repo: Repo) -> None:
    repo.patch(
        "src/sigma/runtime/advance.py",
        """        return AdvanceDecision(
            force_stop=True,
            reason=(""",
        """        return AdvanceDecision(
            continue_round=True,
            reason=(""",
    )


def _ga2(repo: Repo) -> None:
    repo.patch(
        "src/sigma/runtime/advance.py",
        """        if pending == 0:
            return AdvanceDecision()
        self._continues += 1""",
        """        if True:
            return AdvanceDecision()
        self._continues += 1""",
    )


def _ga3(repo: Repo) -> None:
    repo.patch(
        "src/sigma/runtime/event_loop.py",
        """        if self._positive_steer_interval <= 0:
            return []""",
        """        if True:  # 腐化：参数/计数/文案都在，但一次都不注入
            return []
        if self._positive_steer_interval <= 0:
            return []""",
    )


def _ga3b(repo: Repo) -> None:
    repo.patch(
        "src/sigma/runtime/event_loop.py",
        """        dispatch = (
            "需要背景调查且不阻塞主线时，可以派子 agent（task）执行；"
            if "task" in names
            else ""
        )""",
        """        dispatch = "需要背景调查且不阻塞主线时，可以派子 agent（task）执行；\"""",
    )


def main() -> int:
    experiment("GA-4a", "零推进钩子恒返回 continue（抹掉'没人注册'）", _ga4a, TARGETS)
    experiment("GA-4b", "loop 侧无条件询问总线（hooks=None 旧路径被拖崩）", _ga4b, TARGETS)
    experiment("GA-1a", "验证收尾不跑命令恒放行（ax107 型谎报复发）", _ga1a, TARGETS)
    experiment("GA-1b", "连续不过也永不 stopped（死循环烧 token）", _ga1b, TARGETS)
    experiment("GA-2", "清单延续恒不干预（判据形同虚设）", _ga2, TARGETS)
    experiment(
        "GA-3",
        "正向推进注入恒不注入（机制退化成名义开关，症状 B 复发）",
        _ga3,
        POSITIVE_TARGETS,
    )
    experiment(
        "GA-3b",
        "注入文案恒带派发提示（与注册表不同源，模型白调不存在的工具）",
        _ga3b,
        POSITIVE_TARGETS,
    )

    print()
    print("=== GA 注入实验结果（推进机制批次 1+2+3+4） ===")
    ok = True
    for gate, what, passed, detail in RESULTS:
        mark = "✓" if passed else "✗"
        print(f"  {mark} {gate} {what}")
        if detail:
            print(f"      {detail}")
        ok = ok and passed
    print()
    print("全部通过" if ok else "**有实验未过——门槛名不副实，必须修**")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
