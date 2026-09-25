"""``edit`` 工具的测试：正常 / 失败 / 边界（详规 3.5 三态规则）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sigma_ai.base import NeverCancelled
from sigma_agent.types import ToolContext
from sigma_tools.edit import EditTool


def _ctx(root: Path) -> ToolContext:
    return ToolContext(session_id="test", workspace_root=root, signal=NeverCancelled())


async def _run(tool: EditTool, ctx: ToolContext, **kwargs: str):  # type: ignore[no-untyped-def]
    args = tool.params.model_validate(kwargs)
    return await tool.run(args, ctx)


@pytest.mark.asyncio
async def test_edit_replaces_single_occurrence_and_returns_diff(tmp_path: Path) -> None:
    """正常路径：恰好 1 次匹配 → 替换成功，输出含 unified diff。"""
    target = tmp_path / "a.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")

    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="a.py", old_string="return 1", new_string="return 42",
    )

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "def foo():\n    return 42\n"
    # diff 必须可见：模型要能核对"实际改了什么"（diff 行带原缩进）
    text = result.content[0].text
    assert "-    return 1" in text
    assert "+    return 42" in text
    assert result.details["replaced"] is True


@pytest.mark.asyncio
async def test_edit_zero_occurrences_is_error_and_file_untouched(tmp_path: Path) -> None:
    """三态之 0 次：报错并提示先 read，文件保持原样。"""
    target = tmp_path / "a.py"
    target.write_text("return 1\n", encoding="utf-8")

    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="a.py", old_string="return 2", new_string="return 3",
    )

    assert result.is_error
    assert "0 次" in result.content[0].text
    assert "read" in result.content[0].text
    assert target.read_text(encoding="utf-8") == "return 1\n"


@pytest.mark.asyncio
async def test_edit_multiple_occurrences_is_rejected(tmp_path: Path) -> None:
    """三态之 N>1 次：**必须拒绝**，绝不静默替换全部（详规 3.5）。"""
    target = tmp_path / "a.py"
    target.write_text("pass\npass\n", encoding="utf-8")

    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="a.py", old_string="pass", new_string="...",
    )

    assert result.is_error
    assert "2 次" in result.content[0].text
    assert target.read_text(encoding="utf-8") == "pass\npass\n"


@pytest.mark.asyncio
async def test_edit_preserves_crlf_and_matches_lf_style_old_string(tmp_path: Path) -> None:
    """边界：CRLF 文件。模型看到的是 \\n（read 归一化过），必须能匹配上；
    写回时行尾风格**保持 CRLF**，不能全文件换掉。"""
    target = tmp_path / "win.py"
    target.write_bytes(b"value = 1\r\nprint(value)\r\n")

    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="win.py", old_string="value = 1", new_string="value = 2",
    )

    assert not result.is_error
    raw = target.read_bytes()
    assert raw == b"value = 2\r\nprint(value)\r\n"


@pytest.mark.asyncio
async def test_edit_rejects_noop_when_old_equals_new(tmp_path: Path) -> None:
    """边界：old_string == new_string 是空操作，几乎必然是参数搞错，拒绝写入。"""
    target = tmp_path / "a.py"
    target.write_text("keep\n", encoding="utf-8")

    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="a.py", old_string="keep", new_string="keep",
    )

    assert result.is_error
    assert target.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.asyncio
async def test_edit_with_empty_new_string_deletes_text(tmp_path: Path) -> None:
    """边界：new_string 为空 = 删除这段内容，是合法用法。"""
    target = tmp_path / "a.py"
    target.write_text("keep\nDELETE_ME\nkeep2\n", encoding="utf-8")

    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="a.py", old_string="DELETE_ME\n", new_string="",
    )

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "keep\nkeep2\n"


@pytest.mark.asyncio
async def test_edit_missing_file_is_error(tmp_path: Path) -> None:
    """失败路径：文件不存在 → is_error，不抛异常（详规 3.6）。"""
    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="ghost.py", old_string="a", new_string="b",
    )
    assert result.is_error
    assert "不存在" in result.content[0].text


@pytest.mark.asyncio
async def test_edit_mixed_newlines_is_rejected_and_file_untouched(tmp_path: Path) -> None:
    """混合行尾（CRLF 与 LF 共存）必须**拒绝编辑**，文件逐字节不变。

    修复前：`_detect_newline` 看到一处 \\r\\n 就判整文件 CRLF，写回时
    把原本 LF 的行也转成 CRLF——一次只改一行的 edit 产生全文件行尾
    diff（本模块 docstring 声明要避免的"隐性 diff"）。
    处置与多匹配拒绝同一条原则：有歧义时宁可报错，不要猜。
    """
    target = tmp_path / "mixed.txt"
    original = b"line1\r\nline2\nline3\r\n"
    target.write_bytes(original)

    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="mixed.txt", old_string="line2", new_string="LINE2",
    )

    assert result.is_error
    assert "混合行尾" in result.content[0].text
    assert target.read_bytes() == original  # 逐字节不变


@pytest.mark.asyncio
async def test_edit_pure_crlf_still_round_trips(tmp_path: Path) -> None:
    """回归：纯 CRLF 文件**不受影响**——混合判定不能误伤正常文件。"""
    target = tmp_path / "win.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    tool = EditTool()
    result = await _run(
        tool, _ctx(tmp_path),
        path="win.txt", old_string="two", new_string="TWO",
    )

    assert not result.is_error
    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"
