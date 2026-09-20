"""验收门：分层契约必须能拦住违规，也必须放过合法。

为什么两个方向都要断言
    只测「违规树会失败」是没有意义的——契约配置本身写坏时它同样会失败，
    测试依然绿。所以必须同时证明「合法树通过」，
    否则无法区分「契约在工作」和「契约根本没生效」。

为什么每个 fixture 还要断言「只有一条契约坏掉」
    ``layers`` 契约是线性栈，它默认放行所有向下的 import，
    无法表达「同级互不依赖」。所以 ``sigma_tools`` 与 ``sigma_session``
    的兄弟关系必须靠单独的 ``independence`` 契约钉住。
    如果这个样例同时把 layers 也弄坏了，就说明它测的不是兄弟约束。

这个文件对应 docs/architecture.md 第 8 节的门禁，
以及 docs/plans/P0-骨架.md 的验收条件。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "arch"

REAL_CONTRACTS = [
    "分层只允许向下依赖",
    "核心层不得依赖评测与扩展",
    "内置工具与会话层互不依赖",
]


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


def _contract_counts(output: str) -> tuple[int, int]:
    """解析输出末尾的 ``Contracts: N kept, M broken.``"""
    match = re.search(r"Contracts: (\d+) kept, (\d+) broken", output)
    assert match is not None, f"无法解析契约统计：\n{output}"
    return int(match.group(1)), int(match.group(2))


def test_real_tree_passes_all_contracts() -> None:
    """本仓库自身的分层必须合法，且每条契约都被真正执行过。"""
    result = _run_lint_imports(REPO_ROOT)
    output = _combined(result)

    assert result.returncode == 0, output
    for contract in REAL_CONTRACTS:
        assert contract in output, f"契约没有被执行：{contract}"

    kept, broken = _contract_counts(output)
    assert kept == len(REAL_CONTRACTS), f"契约数量不符：{kept}"
    assert broken == 0


@pytest.mark.parametrize(
    ("fixture", "should_pass", "contract"),
    [
        ("clean", True, "fixture layers"),
        ("violating", False, "fixture layers"),
        ("siblings_clean", True, "fixture siblings independent"),
        ("siblings_violating", False, "fixture siblings independent"),
    ],
)
def test_fixture_discriminates(fixture: str, should_pass: bool, contract: str) -> None:
    """合法样例必须过、违规样例必须被拦，且只有目标契约坏掉。"""
    result = _run_lint_imports(FIXTURES / fixture)
    output = _combined(result)

    assert contract in output, f"{fixture}：契约没有被执行\n{output}"

    if should_pass:
        assert result.returncode == 0, f"{fixture}：合法树被误拦\n{output}"
        return

    assert result.returncode != 0, f"{fixture}：违规没有被拦住，契约形同虚设"
    assert f"{contract} BROKEN" in output, f"{fixture}：坏掉的不是目标契约\n{output}"

    _, broken = _contract_counts(output)
    assert broken == 1, f"{fixture}：应当只有一条契约坏掉\n{output}"
