"""影子 git checkpoint 的测试（D5 的 L2）。

这一层的价值全在**回滚能不能真的把工作区恢复成原样**——尤其是
"新建的文件要被删掉"（architecture 6.2 点名的坑）与
"被排除的文件不能被误删"（写宽一点，L2 就变成数据销毁器）。

需要真实 git（开发依赖，不是网络）：git 缺失时整文件 skip——
**"测不了"和"通过了"必须分开**，否则这一层就变成名义边界。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sigma_agent.checkpoint import BUILTIN_EXCLUDES, ShadowCheckpoint

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="本机没有 git，checkpoint 用例未验证"
)


def _cp(tmp_path: Path, **over: object) -> ShadowCheckpoint:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ShadowCheckpoint(
        root=tmp_path / "shadow.git", workspace=ws, **over  # type: ignore[arg-type]
    )


def _ws(tmp_path: Path) -> Path:
    """工作区目录。**这里建目录**——测试里先写文件后建 checkpoint 的顺序很常见，
    让每个用例自己 mkdir 会漏（第一版就漏了一次，症状是 FileNotFoundError）。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


# ----------------------------------------------------------------------
# 基本可用性与快照
# ----------------------------------------------------------------------


def test_available_and_baseline(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    assert cp.available is True

    ref = cp.mark(label="baseline")
    assert ref is not None
    refs = cp.refs()
    assert len(refs) == 1
    assert refs[0].label == "baseline"
    assert refs[0].ref == ref


def test_mark_is_allow_empty(tmp_path: Path) -> None:
    """没有变化的 mark 也要留一条提交——否则"快照数 = 基线 + 写批次数"不成立。

    那条不变量就是门槛 G66 断言的东西：它让"漏打快照"变成一个**可数的差异**，
    而不是"少了一次看不见的备份"。
    """
    cp = _cp(tmp_path)
    cp.mark(label="baseline")
    cp.mark(label="write-batch:write")
    labels = [info.label for info in cp.refs()]
    assert labels == ["write-batch:write", "baseline"]


def test_refs_are_newest_first(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    cp.mark(label="baseline")
    _ws(tmp_path).joinpath("a.txt").write_text("1", encoding="utf-8")
    cp.mark(label="second")
    labels = [info.label for info in cp.refs()]
    assert labels[0] == "second"


# ----------------------------------------------------------------------
# 回滚：恢复修改 / 删除新增（最易漏的一条）
# ----------------------------------------------------------------------


def test_restore_brings_back_modified_file(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    target = ws / "keep.txt"
    target.write_text("原始内容", encoding="utf-8")
    cp = _cp(tmp_path)
    base = cp.mark(label="baseline")
    assert base is not None

    target.write_text("被改坏了", encoding="utf-8")
    report = cp.restore(base)

    assert report.ok is True
    assert target.read_text(encoding="utf-8") == "原始内容"
    assert "keep.txt" in report.changed


def test_restore_deletes_files_added_after_ref(tmp_path: Path) -> None:
    """**architecture 6.2 点名的坑**：`checkout <ref> -- .` 不会删掉新增文件。

    "新建一个文件再把仓库搞坏"是最常见的破坏形态之一——
    不删新增文件，回滚就是假的。
    """
    ws = _ws(tmp_path)
    cp = _cp(tmp_path)
    base = cp.mark(label="baseline")
    assert base is not None

    (ws / "created_later.txt").write_text("新增的", encoding="utf-8")
    report = cp.restore(base)

    assert report.ok is True
    assert not (ws / "created_later.txt").exists()
    assert "created_later.txt" in report.deleted


def test_restore_brings_back_deleted_file(tmp_path: Path) -> None:
    """bash `rm` 掉一个文件之后，回滚要把它找回来。"""
    ws = _ws(tmp_path)
    doomed = ws / "doomed.txt"
    doomed.write_text("别删我", encoding="utf-8")
    cp = _cp(tmp_path)
    base = cp.mark(label="baseline")
    assert base is not None

    doomed.unlink()
    report = cp.restore(base)

    assert report.ok is True
    assert doomed.read_text(encoding="utf-8") == "别删我"


def test_restore_is_itself_reversible(tmp_path: Path) -> None:
    """回滚前自动快照：**回滚错了也能回去**（回滚是人的动作，人也会打错）。"""
    ws = _ws(tmp_path)
    cp = _cp(tmp_path)
    base = cp.mark(label="baseline")
    assert base is not None
    (ws / "a.txt").write_text("写坏的内容", encoding="utf-8")

    report = cp.restore(base)

    assert report.pre_restore_ref is not None
    assert not (ws / "a.txt").exists()
    back = cp.restore(report.pre_restore_ref)
    assert back.ok is True
    assert (ws / "a.txt").read_text(encoding="utf-8") == "写坏的内容"

# ----------------------------------------------------------------------
# 排除：与回滚**无关**的东西不能被卷进来（也不能被误删）
# ----------------------------------------------------------------------


def test_excluded_files_are_not_tracked(tmp_path: Path) -> None:
    """`.gitignore` 里的东西不进快照，`protected` 计数如实上报。"""
    ws = _ws(tmp_path)
    (ws / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (ws / "secret.txt").write_text("不该进快照", encoding="utf-8")
    (ws / "normal.txt").write_text("正常文件", encoding="utf-8")
    cp = _cp(tmp_path)
    base = cp.mark(label="baseline")
    assert base is not None

    tracked = cp._git("ls-files").stdout
    assert "normal.txt" in tracked
    assert "secret.txt" not in tracked

    report = cp.restore(base)
    assert report.ok is True
    # 被排除的文件**不能**被回滚删掉——否则 L2 就变成了数据销毁器
    assert (ws / "secret.txt").exists()


def test_oversize_file_is_excluded_and_survives_restore(tmp_path: Path) -> None:
    """超上限的大文件不入快照，**回滚也不碰它**。"""
    ws = _ws(tmp_path)
    big = ws / "big.bin"
    big.write_bytes(b"x" * 2048)
    cp = _cp(tmp_path, max_file_bytes=1024)
    base = cp.mark(label="baseline")
    assert base is not None

    tracked = cp._git("ls-files").stdout
    assert "big.bin" not in tracked

    report = cp.restore(base)
    assert report.ok is True
    assert big.exists(), "被大小上限排除的文件被回滚删掉了——这是数据销毁"


def test_builtin_excludes_cover_dependency_dirs() -> None:
    """内置清单必须覆盖依赖目录，否则一次快照会慢到不可用（性能就是可用性）。"""
    for name in (".venv/", "node_modules/", "__pycache__/", ".git/"):
        assert name in BUILTIN_EXCLUDES


# ----------------------------------------------------------------------
# 不碰用户仓库自己的 .git（G69）
# ----------------------------------------------------------------------


def test_user_git_repo_is_untouched(tmp_path: Path) -> None:
    """workspace 本身是个 git 仓库时：用户历史里**不能**出现 checkpoint 提交。"""
    ws = _ws(tmp_path)
    (ws / "code.py").write_text("print(1)\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "u", "GIT_AUTHOR_EMAIL": "u@e", "GIT_COMMITTER_NAME": "u",
           "GIT_COMMITTER_EMAIL": "u@e"}
    subprocess.run(["git", "init", "--quiet"], cwd=ws, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "--quiet", "-m", "user commit"], cwd=ws, check=True, env=env)
    before = subprocess.run(
        ["git", "log", "--format=%H"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout

    cp = _cp(tmp_path)
    cp.mark(label="baseline")
    (ws / "code.py").write_text("print(2)\n", encoding="utf-8")
    cp.mark(label="write-batch:edit")

    after = subprocess.run(
        ["git", "log", "--format=%H"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout
    assert after == before, "用户仓库的历史被 checkpoint 污染了"

    user_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True, check=True
    ).stdout
    # 用户看到的只是"文件被改了"，没有多出提交、没有多出目录
    assert "code.py" in user_status


def test_shadow_repo_is_separate_from_workspace(tmp_path: Path) -> None:
    """影子库必须在工作区**之外**（默认在 sessions 目录下）——否则它自己会被快照进去。"""
    cp = _cp(tmp_path)
    cp.mark(label="baseline")
    assert not (_ws(tmp_path) / ".git").exists()
    assert cp.root.exists()


# ----------------------------------------------------------------------
# 工作区配对：这条闸挡住过一次**真事故**（2026-09-22 冒烟）
# ----------------------------------------------------------------------


def test_records_its_workspace(tmp_path: Path) -> None:
    """创建时把工作区记在影子库里——记忆里的配对，不是调用方的记忆。"""
    cp = _cp(tmp_path)
    assert cp.recorded_workspace == _ws(tmp_path).resolve()
    assert cp.workspace_mismatch() == ""


def test_restore_refuses_when_workspace_does_not_match(tmp_path: Path) -> None:
    """**灾难防护**：拿 A 的快照去回滚 B，必须被拒绝，且 B 一个字节都不能动。

    真实事故的形态：CLI 回滚忘了 `--workspace` → 默认当前目录（仓库根）
    → 影子库是给别的工作区建的 → `reset --hard` 把仓库 171 个文件删了。
    所以这条用例断言的不只是"返回 False"，还有**B 的文件仍然在**。
    """
    other = tmp_path / "other-ws"
    other.mkdir()
    victim = other / "keep_me.txt"
    victim.write_text("别删我", encoding="utf-8")

    # 影子库属于 tmp_path/ws（A），却拿去回滚 other-ws（B）
    cp = _cp(tmp_path)
    base = cp.mark(label="baseline")
    assert base is not None
    wrong = ShadowCheckpoint(root=cp.root, workspace=other)

    report = wrong.restore(base)

    assert report.ok is False
    assert "不匹配" in report.note
    assert victim.exists(), "拒绝回滚之后，B 的文件还是被动了——闸门没拦住"


def test_missing_marker_is_refused_not_guessed(tmp_path: Path) -> None:
    """旧版本建的影子库没有标记：**宁可拒绝，不要猜**（猜错就是删文件）。"""
    cp = _cp(tmp_path)
    cp.mark(label="baseline")
    cp._workspace_marker.unlink()

    report = cp.restore(cp.refs()[0].ref)

    assert report.ok is False
    assert "没有记录" in report.note


# ----------------------------------------------------------------------
# 端到端：真 loop + 真 WriteTool + 真 git，破坏之后回滚
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_write_then_rollback_restores_workspace(tmp_path: Path) -> None:
    """把四段接起来验一次：loop 打快照 → 写坏文件 → 回滚 → 工作区回到原样。

    **这是 L2 真正的证据链**：单独测 checkpoint 只能证明"给定 ref 能恢复"，
    证明不了"loop 在写之前真的存了那个 ref"。

    为什么不做成回放场景（详规原本这么写）：
    回放运行器（`evals/runner.py`）的形状是"喂一轮、量一轮"，而回滚是**人的动作**——
    它不是模型跑出来的一步。把它塞进运行器会同时改两件事（测量仪 + 场景格式），
    收益只是"多一行报告"。以集成测试覆盖同一条路径，边界更干净。
    """
    from sigma_agent.agent_messages import LlmMessageWrapper
    from sigma_agent.loop import AgentLoop
    from sigma_agent.registry import ToolRegistry
    from sigma_ai.base import NeverCancelled
    from sigma_ai.fake import FakeProvider
    from sigma_ai.messages import UserMessage
    from sigma_tools.write import WriteTool

    ws = _ws(tmp_path)
    readme = ws / "README.md"
    readme.write_text("原始 README", encoding="utf-8")
    cp = _cp(tmp_path)
    base = cp.mark(label="baseline")
    assert base is not None

    registry = ToolRegistry()
    registry.register(WriteTool())
    provider = FakeProvider.from_rounds(
        [
            [
                {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "call_1",
                    "name": "write",
                    "arguments_delta": '{"path": "README.md", "content": "被写坏了"}',
                },
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            [
                {"type": "text_delta", "text": "改完了", "text_signature": None},
                {"type": "stop", "stop_reason": "stop"},
            ],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        model="fake",
        workspace_root=ws,
        signal=NeverCancelled(),
        clock=lambda: 1_700_000_000,
        checkpoint=cp,
    )

    result = await loop.run_turn(
        [
            LlmMessageWrapper(
                timestamp=1_700_000_000,
                message=UserMessage(content="改一下 README", timestamp=1_700_000_000),
            )
        ]
    )

    assert result.status == "completed"
    assert readme.read_text(encoding="utf-8") == "被写坏了"
    # loop 在写之前打了快照：基线 + write-batch
    labels = [info.label for info in cp.refs()]
    assert any(label.startswith("write-batch:") for label in labels)

    report = cp.restore(base)
    assert report.ok is True
    assert readme.read_text(encoding="utf-8") == "原始 README"



# ----------------------------------------------------------------------
# 失败姿态：保险丝，不是发动机（G70）
# ----------------------------------------------------------------------


def test_unavailable_git_degrades_without_raising(tmp_path: Path) -> None:
    """git 找不到时：available=False、mark 返回 None、**不抛异常**。"""
    cp = _cp(tmp_path, git_bin="definitely-not-a-git-binary")
    assert cp.available is False
    assert "找不到" in cp.unavailable_reason
    assert cp.mark(label="baseline") is None
    assert cp.refs() == []


def test_restore_without_snapshots_reports_failure(tmp_path: Path) -> None:
    """没有任何快照时回滚：返回 ok=False + 说明，而不是崩。"""
    cp = _cp(tmp_path)
    report = cp.restore("deadbeef")
    assert report.ok is False
    assert report.note