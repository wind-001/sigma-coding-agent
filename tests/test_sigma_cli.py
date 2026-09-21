"""CLI 启动分支的测试。

测的是**入口的分派**，不是跑任务——后者要真 provider，由手测与
``scripts/real_api_agent_demo.py`` 覆盖。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sigma import cli as cli_module
from sigma.cli import EXIT_HARNESS_ERROR, main


def test_without_prompt_on_non_interactive_stdin_prints_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W11：既无 ``-p`` 也无 ``-i``，stdin 又不是控制台 → 帮助 + 2，**绝不进 REPL**。

    少了这个判断，`sigma` 在脚本或管道里被调用会**挂住等输入**——
    症状是"卡住"而非报错，排查成本极高。

    这里把 ``run_repl`` 换成假的：既断言它没被调用，
    又保证测试万一判断错了也不会真的挂住。
    """
    entered: list[int] = []

    async def fake_repl(session: object) -> int:
        entered.append(1)
        return 0

    monkeypatch.setattr(cli_module, "stdin_is_interactive", lambda: False)
    monkeypatch.setattr(cli_module, "run_repl", fake_repl)
    # 密钥也要假造：若依赖真实 ~/.sigma/.env，缺 key 时会先返回 2，
    # 于是"没进 REPL"这个断言**无论如何都成立**——那就是一条假绿
    monkeypatch.setattr(
        cli_module, "resolve_api_key", lambda **kwargs: ("sk-test", "测试注入")
    )

    assert main([]) == EXIT_HARNESS_ERROR
    assert entered == [], "非交互 stdin 下进了 REPL——管道调用 sigma 会永久挂住"


def test_devnull_stdin_never_enters_repl() -> None:
    """这是**实测出来的坑**的回归防线。

    Windows 上 ``subprocess.DEVNULL`` / ``< /dev/null`` 是 NUL 设备，
    而 MSVCRT 的 ``isatty()`` 对它返回 **True**（它是字符设备）。
    于是 CI 里调 ``sigma`` 会进 REPL 等输入——不报错、不退出、就是卡着。

    所以判据不能用 ``isatty``，得用 ``GetConsoleMode``。
    这条用例子进程真跑一遍，跨平台都应当返回 2。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "sigma"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    assert proc.returncode == EXIT_HARNESS_ERROR, (
        f"DEVNULL stdin 下没有退到帮助页（exit={proc.returncode}）。"
        f"这通常意味着又退回用 isatty 判交互了——它在 Windows 上会误判 NUL。"
        f"\nstdout:\n{proc.stdout[:300]}"
    )
    assert "sigma -p" in proc.stdout


def test_explicit_interactive_flag_bypasses_stdin_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-i`` 的价值：Git Bash / 管道里 stdin 判不出控制台，靠它显式进入。"""
    entered: list[int] = []

    async def fake_repl(session: object) -> int:
        entered.append(1)
        return 0

    monkeypatch.setattr(cli_module, "stdin_is_interactive", lambda: False)
    monkeypatch.setattr(cli_module, "run_repl", fake_repl)
    monkeypatch.setattr(
        cli_module, "resolve_api_key", lambda **kwargs: ("sk-test", "测试注入")
    )

    assert main(["-i"]) == 0
    assert entered == [1], "-i 被 stdin 检测挡住了——那这个参数就没有意义"


def test_prompt_mode_requires_existing_workspace(tmp_path: Path) -> None:
    """Q4：工作区不存在 = harness 自身失败 → 非 0，与任务成败无关。"""
    missing = tmp_path / "不存在的目录"
    assert main(["-p", "hi", "--workspace", str(missing)]) == EXIT_HARNESS_ERROR
