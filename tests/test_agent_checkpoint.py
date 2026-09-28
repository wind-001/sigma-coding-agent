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

from sigma_ai.stamps import from_epoch as ts
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


def test_mark_skips_when_nothing_changed(tmp_path: Path) -> None:
    """内容没变化的 mark **不新增提交**（G66 修订，P4-批次7 / G859）。

    旧语义是 ``--allow-empty``（快照数=基线+写批次数）；共享库 + plumbing 之后，
    空提交只剩噪声——不变量改成"**每个写批次执行前，分支 tree 与工作区一致**"。
    跳过的 mark 返回现有 tip，refs 不增长。
    """
    cp = _cp(tmp_path)
    first = cp.mark(label="baseline")
    assert first is not None
    again = cp.mark(label="write-batch:write")
    assert again == first, "无变化的第二次 mark 不该新增提交"
    labels = [info.label for info in cp.refs()]
    assert labels == ["baseline"]


def test_mark_commits_when_content_changes(tmp_path: Path) -> None:
    """有变化必须提交：空跳过**不许**把真变化也吞掉（G859 的另一半）。"""
    cp = _cp(tmp_path)
    cp.mark(label="baseline")
    _ws(tmp_path).joinpath("a.txt").write_text("1", encoding="utf-8")
    second = cp.mark(label="write-batch:write")
    assert second is not None
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


def test_workspace_level_repo_is_not_self_snapshotted(tmp_path: Path) -> None:
    """影子库**就在工作区里**（P4-批次7：``<ws>/.sigma/shadow.git``）——
    自指防护由 BUILTIN_EXCLUDES 的 ``.sigma/`` 承担，这里实证它生效。"""
    ws = _ws(tmp_path)
    cp = ShadowCheckpoint(root=ws / ".sigma" / "shadow.git", workspace=ws)
    ref = cp.mark(label="baseline")
    assert ref is not None
    assert cp.root.exists()
    tracked = cp._git("ls-files").stdout
    assert ".sigma" not in tracked, "影子库把自己快照进去了"


def test_shared_repo_dedupes_across_sessions(tmp_path: Path) -> None:
    """共享库跨会话去重（G857）：第二个会话的 baseline 只新增 1 个 commit 对象。

    旧设计（每会话一个全新裸库）在这里会复制全部 blob+tree——
    803MB 的根因。新设计 blob/tree 内容寻址天然共享，只有 commit 是新的。
    """
    ws = _ws(tmp_path)
    (ws / "code.py").write_text("print(1)\n", encoding="utf-8")
    root = tmp_path / "shared.shadow.git"
    cp_a = ShadowCheckpoint(root=root, workspace=ws, branch="session-a")
    cp_b = ShadowCheckpoint(root=root, workspace=ws, branch="session-b")
    ref_a = cp_a.mark(label="baseline-a")
    assert ref_a is not None

    def _object_count() -> int:
        objects_dir = root / "objects"
        return sum(1 for _ in objects_dir.rglob("*") if _.is_file())

    before = _object_count()
    ref_b = cp_b.mark(label="baseline-b")
    assert ref_b is not None
    after = _object_count()
    assert after - before == 1, (
        f"第二个会话的 baseline 新增了 {after - before} 个对象——去重失效"
    )
    # 两个分支各自可见自己的快照
    assert [i.label for i in cp_a.refs()] == ["baseline-a"]
    assert [i.label for i in cp_b.refs()] == ["baseline-b"]


def test_restore_does_not_touch_other_branch_tips(tmp_path: Path) -> None:
    """回滚走 plumbing（read-tree），**不许移动别的会话分支**（G864）。

    ``reset --hard`` 会移动 HEAD 所指分支——共享库里那等于踩坏别的会话。
    这条用例钉住"restore 之后 B 分支的 tip 一个字节都不变"。
    """
    ws = _ws(tmp_path)
    (ws / "f.txt").write_text("1", encoding="utf-8")
    root = tmp_path / "shared.shadow.git"
    cp_a = ShadowCheckpoint(root=root, workspace=ws, branch="session-a")
    cp_b = ShadowCheckpoint(root=root, workspace=ws, branch="session-b")
    base_a = cp_a.mark(label="a1")
    (ws / "f.txt").write_text("2", encoding="utf-8")
    cp_a.mark(label="a2")
    tip_b_before = cp_b.mark(label="b1")
    assert base_a is not None and tip_b_before is not None

    report = cp_a.restore(base_a)
    assert report.ok is True
    assert cp_b.refs()[0].ref == tip_b_before, "B 分支的 tip 被 A 的回滚动了"


def test_gc_packs_objects_and_keeps_refs_readable(tmp_path: Path) -> None:
    """收尾 GC（G863）：打包后 ref 仍可读——``gc.packRefs=false`` 保住
    "松散 ref 恒成立"的不变量（``_head_ref`` 零进程读的前提）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("1", encoding="utf-8")
    cp = ShadowCheckpoint(root=tmp_path / "shadow.git", workspace=ws, branch="s1")
    ref1 = cp.mark(label="m1")
    (ws / "a.txt").write_text("2", encoding="utf-8")
    ref2 = cp.mark(label="m2")
    assert ref1 is not None and ref2 is not None

    assert cp.gc() is True

    packs = list((tmp_path / "shadow.git" / "objects" / "pack").glob("*.pack"))
    assert packs, "gc 之后没有 pack 文件——打包没发生"
    # ref 没被打包进 packed-refs，松散文件仍可直接读
    labels = [info.label for info in cp.refs()]
    assert labels == ["m2", "m1"]
    assert cp._head_ref() == ref2


def test_oversize_scan_is_downsampled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超大文件扫描降频（G862）：首次必扫，之后每 20 次 mark 扫一次。"""
    ws = _ws(tmp_path)
    cp = ShadowCheckpoint(
        root=tmp_path / "shadow.git", workspace=ws, max_file_bytes=1024
    )
    calls = {"n": 0}
    original = cp._collect_oversize

    def _spy() -> bool:
        calls["n"] += 1
        return original()

    monkeypatch.setattr(cp, "_collect_oversize", _spy)
    (ws / "a.txt").write_text("x", encoding="utf-8")
    cp.mark(label="m0")
    for i in range(1, 30):
        (ws / "a.txt").write_text(f"x{i}", encoding="utf-8")
        cp.mark(label=f"m{i}")
    assert calls["n"] == 2, f"30 次 mark 扫了 {calls['n']} 次——降频失效（应为 2：m0 与 m20）"


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
    # baseline 之后、loop 之前的**真实变化**（用户手写的文件）：
    # 这样 loop 的 pre-write 快照才有新内容可存——若 baseline 之后一切未变，
    # pre-write 快照与 baseline 内容一致，被空跳过（G859）是正确行为。
    (ws / "notes.txt").write_text("用户手写的笔记", encoding="utf-8")

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
        clock=lambda: ts(1_700_000_000),
        checkpoint=cp,
    )

    result = await loop.run_turn(
        [
            LlmMessageWrapper(
                timestamp=ts(1_700_000_000),
                message=UserMessage(content="改一下 README", timestamp=ts(1_700_000_000)),
            )
        ]
    )

    assert result.status == "completed"
    assert readme.read_text(encoding="utf-8") == "被写坏了"
    # loop 在写之前打了快照（含用户的 notes.txt）：
    labels = [info.label for info in cp.refs()]
    assert any(label.startswith("write-batch:") for label in labels)

    report = cp.restore(base)
    assert report.ok is True
    assert readme.read_text(encoding="utf-8") == "原始 README"
    assert not (ws / "notes.txt").exists(), "回滚没删掉 baseline 之后新增的文件"



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


def test_restore_refused_when_pre_snapshot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自保快照失败 → **拒绝回滚**，工作区原样不动（2026-09-24 review 修复）。

    mark 的保险丝语义（失败不阻断）在回滚场景是错的：``reset --hard``
    删不掉未进快照的新增文件——快照失败还继续 reset，回滚不完整却报
    ok=True（"看似回滚了"）。**回滚的优先级高于"不阻断"。**
    """
    ws = _ws(tmp_path)
    cp = _cp(tmp_path)
    base = cp.mark(label="baseline")
    assert base is not None
    (ws / "created_later.txt").write_text("新增的", encoding="utf-8")

    monkeypatch.setattr(cp, "mark", lambda **kw: None)
    report = cp.restore(base)

    assert report.ok is False
    assert "拒绝回滚" in report.note
    # 工作区一个字节都不能动——与"工作区不匹配拒绝回滚"是同一条闸门纪律
    assert (ws / "created_later.txt").exists()