"""验收门：分层契约必须能拦住违规，也必须放过合法。

为什么两个方向都要断言
    只测「违规树会失败」是没有意义的——契约配置本身写坏时它同样会失败，
    测试依然绿。所以必须同时证明「合法树通过」，
    否则无法区分「契约在工作」和「契约根本没生效」。

这个文件对应 docs/architecture.md 第 8 节的第一道门禁，
以及 docs/plans/P0-骨架.md 的验收条件。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "arch"

REAL_LAYER_CONTRACT = "分层只允许向下依赖"
REAL_FORBIDDEN_CONTRACT = "核心层不得依赖评测与扩展"
FIXTURE_CONTRACT = "fixture layers"


def _lint_imports_exe() -> str:
    """定位 lint-imports 可执行文件。

    直接调用 venv 里的 python 时，venv 的 Scripts 目录不一定在 PATH 上，
    所以优先看 sys.executable 旁边有没有。
    """
    suffix = ".exe" if os.name == "nt" else ""
    beside = Path(sys.executable).parent / f"lint-imports{suffix}"
    if beside.exists():
        return str(beside)

    found = shutil.which("lint-imports")
    if found is None:
        raise RuntimeError(
            "找不到 lint-imports。请先执行：pip install -e '.[dev]'"
        )
    return found


def _run_lint_imports(cwd: Path) -> subprocess.CompletedProcess[str]:
    """在指定目录下运行 lint-imports。"""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(cwd) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [_lint_imports_exe()],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


def test_real_tree_passes_all_contracts() -> None:
    """本仓库自身的分层必须合法，且两条契约都被真正执行过。"""
    result = _run_lint_imports(REPO_ROOT)
    output = _combined(result)

    assert result.returncode == 0, output
    assert REAL_LAYER_CONTRACT in output, "分层契约没有被执行"
    assert REAL_FORBIDDEN_CONTRACT in output, "禁止依赖契约没有被执行"


def test_clean_fixture_passes() -> None:
    """合法树必须通过——否则无法证明契约不是在无差别报错。"""
    result = _run_lint_imports(FIXTURES / "clean")
    output = _combined(result)

    assert result.returncode == 0, output
    assert FIXTURE_CONTRACT in output, "fixture 契约没有被执行"


def test_violating_fixture_is_blocked() -> None:
    """违规树必须被拦，且报出违规的那一层。"""
    result = _run_lint_imports(FIXTURES / "violating")
    output = _combined(result)

    assert result.returncode != 0, "反向 import 没有被拦住，契约形同虚设"
    assert FIXTURE_CONTRACT in output, "没有报出是哪个契约被违反"
    assert "layera" in output, "没有报出违规模块"
