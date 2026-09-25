"""``write`` / ``read`` 工具的测试（2026-09-24 review 修复的回归）。

- write：行尾保真——文本模式在 Windows 上会把 ``\\n`` 静默转成 ``\\r\\n``，
  模型写的 LF 落盘变 CRLF 就是"文件字节被悄悄改变"。
- read：行范围越界必须**报错**——静默返回空会让模型无法区分
  "文件是空的"与"我给的行范围越界了"（两者的纠正动作完全不同）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sigma_agent.types import ToolContext
from sigma_ai.base import NeverCancelled
from sigma_tools.read import ReadTool
from sigma_tools.write import WriteTool


def _ctx(root: Path) -> ToolContext:
    return ToolContext(session_id="test", workspace_root=root, signal=NeverCancelled())


@pytest.mark.asyncio
async def test_write_preserves_lf_line_endings(tmp_path: Path) -> None:
    """写入的 ``\\n`` 落盘后必须还是 ``\\n``——Windows 上不得变成 CRLF。

    修复前用 ``write_text``（文本模式默认换行转换），Windows 上每个 \\n
    都落成 \\r\\n，且 details 里的 new_bytes 统计与实际落盘字节数不符。
    """
    tool = WriteTool()
    args = tool.params.model_validate({"path": "a.py", "content": "x = 1\ny = 2\n"})
    result = await tool.run(args, _ctx(tmp_path))

    assert not result.is_error
    raw = (tmp_path / "a.py").read_bytes()
    assert raw == b"x = 1\ny = 2\n"
    assert result.details["new_bytes"] == len(raw)


@pytest.mark.asyncio
async def test_read_start_line_beyond_total_is_error(tmp_path: Path) -> None:
    """``start_line`` 超出总行数 → is_error，且文案必须报出总行数（给出路）。"""
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    tool = ReadTool()
    args = tool.params.model_validate({"path": "a.txt", "start_line": 10})
    result = await tool.run(args, _ctx(tmp_path))

    assert result.is_error
    assert "3 行" in result.content[0].text


@pytest.mark.asyncio
async def test_read_inverted_range_is_error(tmp_path: Path) -> None:
    """``start_line > end_line`` → is_error，不能静默返回空文本。"""
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    tool = ReadTool()
    args = tool.params.model_validate({"path": "a.txt", "start_line": 3, "end_line": 1})
    result = await tool.run(args, _ctx(tmp_path))

    assert result.is_error
    assert "颠倒" in result.content[0].text


@pytest.mark.asyncio
async def test_read_valid_range_still_works(tmp_path: Path) -> None:
    """回归：合法行范围不受越界校验影响。"""
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    tool = ReadTool()
    args = tool.params.model_validate({"path": "a.txt", "start_line": 2, "end_line": 3})
    result = await tool.run(args, _ctx(tmp_path))

    assert not result.is_error
    assert "2\ttwo" in result.content[0].text
    assert "3\tthree" in result.content[0].text


@pytest.mark.asyncio
async def test_read_empty_file_is_not_an_error(tmp_path: Path) -> None:
    """回归：空文件是**合法结果**（没给行范围时不应被越界校验误伤）。"""
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")

    tool = ReadTool()
    args = tool.params.model_validate({"path": "empty.txt"})
    result = await tool.run(args, _ctx(tmp_path))

    assert not result.is_error
