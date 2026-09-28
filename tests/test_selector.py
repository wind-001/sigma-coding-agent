"""交互决策按键化的门槛测试(P3-批次2 追加):G111–G113。

G111 `_move_selection`:Tab/方向键 → 状态的纯函数映射。
G112 `select_option`:pipe input 驱动的真实按键流(Tab 切换/数字直选/Esc 取消)。
G113 审批 chooser 语义:三选项 + 推荐;取消(Esc/None)按拒绝;allowlist 写入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys

from sigma._approval import Allowlist, CliApprovalGate, allowlist_key
from sigma._selector import _move_selection, choose_option, select_option
from sigma_agent.hooks import ApprovalDecision, ApprovalHook


# ---------------------------------------------------------------------------
# G111:按键 → 状态的纯函数
# ---------------------------------------------------------------------------


def test_move_selection_wraps_and_ignores_unknown() -> None:
    assert _move_selection(0, 3, "down") == 1
    assert _move_selection(1, 3, "tab") == 2
    assert _move_selection(2, 3, "controli") == 0  # Tab=ControlI,环绕
    assert _move_selection(0, 3, "up") == 2
    assert _move_selection(2, 3, "left") == 1
    assert _move_selection(1, 3, "f5") == 1  # 未知键不动
    assert _move_selection(0, 0, "down") == 0  # 空选项不崩


# ---------------------------------------------------------------------------
# G112:pipe input 驱动的选择器
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_tab_then_enter_picks_next() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\t\n")  # Tab → 下一项;Enter 确认
        index = await select_option(
            ["标题"],
            ["方案A", "方案B", "方案C"],
            recommended_index=0,
            input_factory=lambda: pipe,
            interactive=True,
        )
    assert index == 1


@pytest.mark.asyncio
async def test_select_arrow_up_and_enter() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[A\n")  # ↑ 环绕到最后一项;确认
        index = await select_option(
            ["标题"],
            ["A", "B", "C"],
            recommended_index=0,
            input_factory=lambda: pipe,
            interactive=True,
        )
    assert index == 2


@pytest.mark.asyncio
async def test_select_digit_direct_pick() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("3\n")
        index = await select_option(
            ["标题"],
            ["A", "B", "C"],
            recommended_index=None,
            input_factory=lambda: pipe,
            interactive=True,
        )
    assert index == 2


@pytest.mark.asyncio
async def test_select_ctrl_c_cancels() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")
        index = await select_option(
            ["标题"],
            ["A", "B"],
            recommended_index=0,
            input_factory=lambda: pipe,
            interactive=True,
        )
    assert index is None


@pytest.mark.asyncio
async def test_select_non_interactive_returns_none() -> None:
    """interactive=False(评测/管道)→ None,调用方走编号降级;不碰终端。"""
    index = await select_option(
        ["标题"],
        ["A", "B"],
        recommended_index=0,
        interactive=False,
    )
    assert index is None


@pytest.mark.asyncio
async def test_choose_option_typed_fallback_parses_numbers(tmp_path: Path) -> None:
    """非 TTY 降级:编号输入解析;空/非法 → None。"""
    answers = iter(["2", "", "乱敲", "B"])

    async def fallback(prompt: str) -> str:
        return next(answers)

    common: dict[str, Any] = dict(
        recommended_index=0,
        interactive=False,
        interactive_fallback=fallback,
    )
    assert await choose_option(["标题"], ["A", "B"], **common) == 1
    assert await choose_option(["标题"], ["A", "B"], **common) is None  # 空 = 取消
    assert await choose_option(["标题"], ["A", "B"], **common) is None  # 非法 = 取消
    assert await choose_option(["标题"], ["A", "B"], **common) == 1  # 选项原文也可


# ---------------------------------------------------------------------------
# G113:审批 chooser 语义
# ---------------------------------------------------------------------------


class _RecordingChooser:
    """记录 (title, options, recommended),按脚本回下标。"""

    def __init__(self, answers: list[int | None]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[list[str], list[str], int | None]] = []

    async def __call__(
        self, title_lines: list[str], options: list[str], recommended_index: int | None
    ) -> int | None:
        self.calls.append((title_lines, options, recommended_index))
        if self._answers:
            return self._answers.pop(0)
        return None


def _gate(tmp_path: Path, chooser: _RecordingChooser) -> CliApprovalGate:
    return CliApprovalGate(
        workspace=tmp_path,
        chooser=chooser,
        allowlist=Allowlist(tmp_path / ".sigma" / "allowlist.json"),
    )


@pytest.mark.asyncio
async def test_approval_options_and_default_recommendation(tmp_path: Path) -> None:
    chooser = _RecordingChooser([0])
    gate = _gate(tmp_path, chooser)

    decision = await gate.approve("bash", {"command": "sudo reboot"}, "call_1")

    assert decision.allowed is True
    (title, options, recommended) = chooser.calls[0]
    assert options == ["允许一次", "总是允许(写入 .sigma/allowlist.json)", "拒绝"]
    assert recommended == 0  # 默认光标在"允许一次"(星辰拍板)
    assert any("sudo" in line for line in title)
    assert any("后果" in line for line in title)


@pytest.mark.asyncio
async def test_approval_always_allow_writes_allowlist(tmp_path: Path) -> None:
    chooser = _RecordingChooser([1])
    gate = _gate(tmp_path, chooser)

    decision = await gate.approve("bash", {"command": "chmod 777 x"}, "call_1")

    assert decision.allowed is True
    assert "allowlist" in decision.note
    key = allowlist_key("bash", {"command": "chmod 777 x"}, tmp_path)
    assert gate.allowlist.contains(key)


@pytest.mark.asyncio
async def test_approval_cancel_counts_as_denial(tmp_path: Path) -> None:
    """Esc / Ctrl+C / EOF(chooser 返回 None)→ 拒绝,原因进工具结果。"""
    chooser = _RecordingChooser([None])
    gate = _gate(tmp_path, chooser)

    decision = await gate.approve("bash", {"command": "sudo reboot"}, "call_1")

    assert decision.allowed is False
    assert "sudo" in decision.reason


# ---------------------------------------------------------------------------
# 显示回归:重画的选项区必须逐行可见(真机截图 bug)
# ---------------------------------------------------------------------------


def test_draw_options_paints_every_option_on_its_own_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """回归:行间漏了换行 → 每个选项覆盖前一个,只剩最后一条可见。"""
    from sigma._selector import _draw_options

    _draw_options(
        ["方案A", "方案B", "方案C", "方案D"],
        index=3,
        recommended_index=3,
        final=False,
    )
    out = capsys.readouterr().out
    # 四个选项**逐行**都在:每个选项行以换行结束
    assert "› [4] 方案D" in out
    for position in (1, 2, 3, 4):
        marker = f"[{position}]"
        assert marker in out
    lines_with_options = [ln for ln in out.splitlines() if "[1]" in ln or "[2]" in ln or "[3]" in ln or "[4]" in ln]
    assert len(lines_with_options) == 4, f"4 个选项应各占一行,实际:{lines_with_options}"


def test_draw_options_truncates_to_terminal_width(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """超宽选项截断——折行会让"光标上移 N 行"对不齐,重画叠加错位。"""
    import os
    import shutil

    from sigma._selector import _draw_options

    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((60, 24)))
    long_option = "很长很长的方案" * 30
    _draw_options([long_option], index=0, recommended_index=None, final=False)
    out = capsys.readouterr().out
    for line in out.splitlines():
        assert len(line) <= 80, f"超宽行未截断:{len(line)}"
