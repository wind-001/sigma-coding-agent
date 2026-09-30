"""快门：组件级开发循环用的分级门禁（秒级~1 分钟）。

分层（AGENTS.md 第 8 条）::

    组件级  →  scripts/fast_gate.py   契约检查 + 改动相关的测试
    批次级  →  全量门：pytest 全量 + mypy src + lint-imports
    兜底    →  CI（推送后自动）

用法::

    ./.venv/Scripts/python.exe scripts/fast_gate.py            # 自动按 git 改动推导
    ./.venv/Scripts/python.exe scripts/fast_gate.py tests/test_tools_bash.py
    ./.venv/Scripts/python.exe scripts/fast_gate.py bash       # 关键字匹配测试文件

推导规则：git 改动里
  - tests/ 下的文件直接跑；
  - src/sigma/**/m.py / evals/*.py 按模块名（去 test_ 前缀）模糊匹配 tests/ 文件名；
  - 一个都匹配不上就提示显式给参数（不静默跑全量——快门的价值就是快，
    该跑全量的时候去批次边界跑）。

快门**不替代**全量门：提交前/合并前/结构或依赖变更后仍必须跑全量
（约 6 分钟，¥0）。这两层的分工写在 AGENTS.md 第 8 条。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"


def changed_files() -> list[str]:
    """未提交改动（含暂存）的文件清单。"""
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    files: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:  # 重命名行取新路径
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


def derive_tests(args: list[str]) -> list[str]:
    """从参数或 git 改动推导要跑的测试文件。

    搜索池 = 仓库 tests/。**数据集的 judge_tests 不在池里**——它们要在
    任务工作区内跑（模块导入以工作区为根），验题用::

        ./.venv/Scripts/python.exe evals/task_runner.py --task syn-XXX --check-only

    匹配为空**必须报错退出**，绝不静默回退全量——那会毁掉快门的意义。
    """
    pool = sorted(str(p).replace("\\", "/") for p in (REPO / "tests").glob("test_*.py"))

    picked: list[str] = []
    for a in args:
        if a.startswith("syn-"):
            print(f"[快门] ✗ {a} 是数据集任务，验题用 check-only：")
            print(f"       .venv/Scripts/python.exe evals/task_runner.py --task {a} --check-only")
            raise SystemExit(2)
        if a.endswith(".py") and Path(a).exists():
            picked.append(a)
        else:
            picked += [t for t in pool if a in Path(t).stem]
    if args:
        if not picked:
            print("[快门] ✗ 参数没匹配到任何测试文件——显式给路径或关键字，")
            print("       全量门去批次边界跑（快门静默回退全量 = 快门白做）。")
            raise SystemExit(2)
        return sorted(set(picked))

    # 自动推导模式：与 git 改动求交
    changed = changed_files()
    related: list[str] = []
    for f in changed:
        if f.startswith("tests/") and f.endswith(".py"):
            related.append(f)
            continue
        stem = Path(f).stem.lstrip("_")
        if f.startswith(("src/sigma/", "evals/", "scripts/")):
            related += [t for t in pool if stem in Path(t).stem]
    if not related:
        print("[快门] ✗ git 改动与任何测试文件都对不上——显式给参数，或跑全量门。")
        for f in changed[:20]:
            print("       -", f)
        raise SystemExit(2)
    return sorted(set(related))


def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=REPO, text=True, encoding="utf-8", errors="replace", **kw)  # type: ignore[arg-type]


def main() -> int:
    args = sys.argv[1:]
    tests = derive_tests(args)
    print(f"快门范围：{len(tests)} 个测试文件")
    for t in tests:
        print("  -", t)

    # 第一道：import-linter（秒级，结构契约不许等批次）
    r = subprocess.run(
        [str(PY), "-c",
         "from importlinter.cli import lint_imports_command; "
         "raise SystemExit(lint_imports_command())"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        print(r.stdout)
        print("[快门] ✗ 分层契约破了")
        return 1
    print("[快门] ✓ 分层契约 KEPT")

    # 第二道：改动相关的测试
    r = run([str(PY), "-m", "pytest", "-q", *tests])
    if r.returncode != 0:
        print("[快门] ✗ 相关测试有红")
        return 1
    print(f"[快门] ✓ {len(tests)} 个测试文件全绿（全量门在批次边界/CI 仍必跑）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
