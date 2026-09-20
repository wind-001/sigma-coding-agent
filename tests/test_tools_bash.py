"""``bash`` 工具的测试：正常 / 失败 / 边界（超时是详规 3.5 的硬要求）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sigma_ai.base import NeverCancelled
from sigma_agent.types import ToolContext
from sigma_tools.bash import BashTool


def _ctx(root: Path) -> ToolContext:
    return ToolContext(session_id="test", workspace_root=root, signal=NeverCancelled())


async def _run(tool: BashTool, ctx: ToolContext, **kwargs: object):  # type: ignore[no-untyped-def]
    args = tool.params.model_validate(kwargs)
    return await tool.run(args, ctx)


@pytest.mark.asyncio
async def test_bash_runs_command_and_returns_stdout(tmp_path: Path) -> None:
    """正常路径：stdout 进 content，退出码进 details（不占 content 的 token）。"""
    tool = BashTool()
    result = await _run(tool, _ctx(tmp_path), command="echo sigma_ok")

    assert not result.is_error
    assert "sigma_ok" in result.content[0].text
    assert result.details["exit_code"] == 0


@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_error_with_stderr_in_content(tmp_path: Path) -> None:
    """失败路径：非零退出 → is_error=True，stderr **必须进 content**（详规 3.6）。"""
    tool = BashTool()
    result = await _run(
        tool, _ctx(tmp_path), command="echo bad_thing >&2; exit 3"
    )

    assert result.is_error
    text = result.content[0].text
    assert "退出码 3" in text
    assert "bad_thing" in text
    assert result.details["exit_code"] == 3


@pytest.mark.asyncio
async def test_bash_timeout_kills_command(tmp_path: Path) -> None:
    """边界：超时**必须**生效——否则一条挂起命令让 loop 永久卡死（详规 3.5）。"""
    tool = BashTool()
    result = await _run(
        tool, _ctx(tmp_path), command="sleep 30", timeout_s=1
    )

    assert result.is_error
    assert "超时" in result.content[0].text
    assert result.details["timed_out"] is True


@pytest.mark.asyncio
async def test_bash_cwd_resolved_relative_to_workspace(tmp_path: Path) -> None:
    """边界：cwd 相对路径基于工作区根目录解析。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "marker.txt").write_text("x", encoding="utf-8")

    tool = BashTool()
    result = await _run(tool, _ctx(tmp_path), command="ls", cwd="sub")

    assert not result.is_error
    assert "marker.txt" in result.content[0].text


@pytest.mark.asyncio
async def test_bash_missing_bash_binary_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败路径：找不到 bash 就报错——**不回退到 cmd.exe**（语义会悄悄改变）。"""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    tool = BashTool()
    result = await _run(tool, _ctx(tmp_path), command="ls")

    assert result.is_error
    assert "bash" in result.content[0].text


@pytest.mark.asyncio
async def test_bash_bad_cwd_is_error(tmp_path: Path) -> None:
    """失败路径：cwd 不存在 → is_error，不静默落到别的目录执行。"""
    tool = BashTool()
    result = await _run(tool, _ctx(tmp_path), command="ls", cwd="no_such_dir")

    assert result.is_error
    assert "不存在" in result.content[0].text
