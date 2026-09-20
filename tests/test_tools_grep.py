"""``grep`` 工具的测试：正常 / 失败 / 边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sigma_ai.base import NeverCancelled
from sigma_agent.types import ToolContext
from sigma_tools.grep import GrepTool


def _ctx(root: Path) -> ToolContext:
    return ToolContext(session_id="test", workspace_root=root, signal=NeverCancelled())


async def _run(tool: GrepTool, ctx: ToolContext, **kwargs: str):  # type: ignore[no-untyped-def]
    args = tool.params.model_validate(kwargs)
    return await tool.run(args, ctx)


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    """典型小工作区：两个源码文件 + 一个 .git 内的文件 + 一个二进制文件。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\nTARGET = find_me\ny = 2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("other\nTARGET = find_me_too\n", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "packed").write_text("find_me_in_git\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    return tmp_path


@pytest.mark.asyncio
async def test_grep_returns_path_line_and_text(ws: Path) -> None:
    """正常路径：输出必须是 文件:行号:文本（详规 3.5），且相对路径省 token。"""
    tool = GrepTool()
    result = await _run(tool, _ctx(ws), pattern="find_me")

    assert not result.is_error
    text = result.content[0].text
    assert "src/a.py:2:" in text
    assert "b.py:2:" in text
    assert result.details["matches"] == 2


@pytest.mark.asyncio
async def test_grep_skips_git_dir_and_binary_but_counts_them(ws: Path) -> None:
    """边界：.git 不搜；二进制跳过但**计数上报**——静默跳过会被当成"没有"。"""
    tool = GrepTool()
    result = await _run(tool, _ctx(ws), pattern="find_me")

    text = result.content[0].text
    assert ".git" not in text
    assert "blob.bin" not in text
    assert result.details["files_skipped"] == 1
    assert "跳过" in text


@pytest.mark.asyncio
async def test_grep_glob_filters_by_filename(ws: Path) -> None:
    """边界：glob 按文件名过滤，只搜 *.py。"""
    (ws / "notes.txt").write_text("find_me_in_txt\n", encoding="utf-8")

    tool = GrepTool()
    result = await _run(tool, _ctx(ws), pattern="find_me", glob="*.py")

    text = result.content[0].text
    assert "notes.txt" not in text
    assert result.details["matches"] == 2


@pytest.mark.asyncio
async def test_grep_no_match_is_not_an_error(ws: Path) -> None:
    """边界：无匹配是合法结果（不是工具失败），模型据此换 pattern。"""
    tool = GrepTool()
    result = await _run(tool, _ctx(ws), pattern="zzz_never")

    assert not result.is_error
    assert "没有匹配" in result.content[0].text
    assert result.details["matches"] == 0


@pytest.mark.asyncio
async def test_grep_invalid_regex_is_error(ws: Path) -> None:
    """失败路径：正则非法 → is_error，让模型自己修 pattern（详规 3.6）。"""
    tool = GrepTool()
    result = await _run(tool, _ctx(ws), pattern="([unclosed")

    assert result.is_error
    assert "正则" in result.content[0].text


@pytest.mark.asyncio
async def test_grep_single_file_scope(ws: Path) -> None:
    """边界：path 指向单个文件时只搜该文件。"""
    tool = GrepTool()
    result = await _run(tool, _ctx(ws), pattern="find_me", path="b.py")

    text = result.content[0].text
    assert "b.py:2:" in text
    assert "src/a.py" not in text


@pytest.mark.asyncio
async def test_grep_missing_path_is_error(ws: Path) -> None:
    """失败路径：搜索路径不存在 → is_error。"""
    tool = GrepTool()
    result = await _run(tool, _ctx(ws), pattern="x", path="no_such_dir")
    assert result.is_error
