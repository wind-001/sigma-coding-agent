"""推进机制批次 1 门槛注入实验：GA-4 证伪扩展面的零值承诺。

为什么必须做这个（P0 核心教训，已固化为验收门）
    「配置跑绿」不等于「约束生效」。一条门槛如果从来没有红过，就不知道
    它到底在不在工作。本脚本对 AdvanceHook 扩展面的两条承诺各做一次
    **注入实验**：故意把被防的问题造出来，确认对应断言真的红，
    然后还原、确认真的绿。

两条承诺与各自的注入靶
    GA-4a  零推进钩子 → advance_decide 返回 None
           注入：把 "return None" 改成恒返回 continue 决定
           （腐化形状：钩子面在，但"没人注册"这件事被抹掉——
           评测/回放/子会话不再静默，全部被强行续命）
           必须红：test_advance_decide_returns_none_without_hooks

    GA-4b  loop 侧只在有总线时才问（hooks=None 的旧路径不被碰）
           注入：把 "if self._hooks is not None:" 改成 "if True:"
           （腐化形状：直接构造 AgentLoop 的旧测试/回放路径被拖去问一个
           不存在的总线 → AttributeError）
           必须红：test_loop_without_advance_hooks_is_byte_identical
           （或同文件的 hooks=None 用例）

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
TARGET = "tests/test_advance_hook.py"


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

    def run(self, target: str) -> tuple[int, str]:
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


RESULTS: list[tuple[str, str, bool, str]] = []


def experiment(gate: str, what: str, inject) -> None:
    repo = Repo()
    try:
        # 1. 基线必须绿——否则"注入后变红"说明不了任何事
        code, out = repo.run(TARGET)
        if code != 0:
            RESULTS.append(
                (gate, what, False, f"基线不是绿的，实验无效：{out.strip()[-200:]}")
            )
            return

        # 2. 注入
        inject(repo)

        # 3. 必须变红
        code, out = repo.run(TARGET)
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
        code, out = repo.run(TARGET)
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
                    decision = await self._hooks.advance_decide(""",
        """                if True:
                    decision = await self._hooks.advance_decide(""",
    )


def main() -> int:
    experiment("GA-4a", "零推进钩子恒返回 continue（抹掉'没人注册'）", _ga4a)
    experiment("GA-4b", "loop 侧无条件询问总线（hooks=None 旧路径被拖崩）", _ga4b)

    print()
    print("=== GA-4 注入实验结果（推进机制批次 1：扩展面零值承诺） ===")
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
