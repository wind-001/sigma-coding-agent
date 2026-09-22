"""L1 写路径约束的测试（D5 / architecture 6.1）。

这一层的价值全在**符号链接**那一条：文本层面的 `is_relative_to` 看着能拦住
`../x`，却完全拦不住 `workspace/link -> /etc`——而后者才是真正会写到外面的形态。

全部离线、免 key；用真实文件系统（不 mock pathlib——那会把被测逻辑一起 mock 掉）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sigma_agent.types import ToolContext
from sigma_ai.base import NeverCancelled
from sigma_tools._paths import (
    PathEscapesWorkspace,
    resolve_path,
    resolve_write_path,
)


def _ctx(root: Path) -> ToolContext:
    return ToolContext(session_id="test", workspace_root=root, signal=NeverCancelled())


# ----------------------------------------------------------------------
# 正常路径必须照常工作（边界做成了障碍同样是失败）
# ----------------------------------------------------------------------


def test_writes_inside_workspace_are_allowed(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)

    assert resolve_write_path(_ctx(ws), "a.txt") == (ws / "a.txt").resolve()
    assert resolve_write_path(_ctx(ws), "src/b.txt") == (ws / "src" / "b.txt").resolve()
    # 新建（还不存在）的路径必须能过——否则"新建文件"这条最普通的路径反而走不通
    assert resolve_write_path(_ctx(ws), "new/deep/c.txt").parent == (ws / "new" / "deep").resolve()
    # 工作区根自己也算在界内
    assert resolve_write_path(_ctx(ws), ".") == ws.resolve()


def test_absolute_path_inside_workspace_is_allowed(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    assert resolve_write_path(_ctx(ws), str(ws / "x.txt")) == (ws / "x.txt").resolve()


# ----------------------------------------------------------------------
# 越界必须被拦
# ----------------------------------------------------------------------


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(PathEscapesWorkspace):
        resolve_write_path(_ctx(ws), "../outside.txt")


def test_traversal_through_subdir_is_rejected(tmp_path: Path) -> None:
    """`sub/../../x` 归一化之后在外——只看前缀的写法会放它过去。"""
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    with pytest.raises(PathEscapesWorkspace):
        resolve_write_path(_ctx(ws), "sub/../../outside.txt")


def test_absolute_path_outside_is_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    with pytest.raises(PathEscapesWorkspace):
        resolve_write_path(_ctx(ws), str(outside))


def test_sibling_prefix_directory_is_rejected(tmp_path: Path) -> None:
    """`ws-evil` 与 `ws` 只差一个前缀——**朴素 startswith 会把它判成界内**。

    这条用例钉的就是"必须比 `root + 分隔符`"这一步：
    少了它，`/tmp/ws-evil/x` 会被当成 `/tmp/ws/x`。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    evil = tmp_path / "ws-evil"
    evil.mkdir()
    with pytest.raises(PathEscapesWorkspace):
        resolve_write_path(_ctx(ws), str(evil / "x.txt"))


def test_error_message_gives_a_way_out(tmp_path: Path) -> None:
    """越界文案必须给出路（同 truncate.py 的纪律：只说"不行"会让模型原地重试）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(PathEscapesWorkspace) as info:
        resolve_write_path(_ctx(ws), "../x.txt")
    message = str(info.value)
    assert "工作区" in message
    assert "--workspace" in message


# ----------------------------------------------------------------------
# 符号链接：这一层真正要防的东西
# ----------------------------------------------------------------------


def _make_dir_link(link: Path, target: Path) -> bool:
    """建一个目录链接。Windows 上 symlink 要权限，用 junction 兜底。

    返回是否建成——建不成时用例应当 skip，而不是假装通过。
    **"测不了"和"通过了"必须分开**，否则这一层就变成名义边界。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import subprocess

        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        return done.returncode == 0
    return False


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """工作区里的链接指向外部：文本上"在界内"，resolve 之后在界外。

    这是 L1 存在的核心理由——`_paths.py` 的旧 docstring 明确说过
    "顺手加一句 is_relative_to 是名义防护"，这条用例就是那句话的正解。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ws / "escape"
    if not _make_dir_link(link, outside):
        pytest.skip("本机无法创建目录链接（需要权限），符号链接用例未验证")

    with pytest.raises(PathEscapesWorkspace):
        resolve_write_path(_ctx(ws), "escape/pwned.txt")


def test_symlink_inside_workspace_is_allowed(tmp_path: Path) -> None:
    """指向**界内**的链接不该被误拦：误拦比漏拦更让人无路可走。"""
    ws = tmp_path / "ws"
    (ws / "real").mkdir(parents=True)
    link = ws / "alias"
    if not _make_dir_link(link, ws / "real"):
        pytest.skip("本机无法创建目录链接（需要权限）")

    resolved = resolve_write_path(_ctx(ws), "alias/ok.txt")
    assert resolved.parent == (ws / "real").resolve()


# ----------------------------------------------------------------------
# 读路径**不受**约束（architecture 6.3：L1 只约束写）
# ----------------------------------------------------------------------


def test_read_path_is_not_constrained(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"

    resolved = resolve_path(_ctx(ws), str(outside))
    assert resolved == outside

    # 相对路径仍以工作区为基准解析（只是不做边界检查）
    assert resolve_path(_ctx(ws), "a.txt") == ws / "a.txt"