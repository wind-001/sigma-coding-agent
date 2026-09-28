"""影子库路径迁移与水位治理的测试(P4-批次8,星辰拍板 2026-09-27)。

G119 迁移:旧库 ``.sigma/shadow.git`` 同盘 rename 到 ``.sigma/session/shadow.git``,
回滚点零丢失,幂等。
G120 水位触发:超限按最旧优先删**非当前**分支;闲置不足的分支被保护(R4);
    当前会话的分支在任何一级都不动。
G121 空间回落:砍尾 + ``gc --prune=now`` 之后不可达对象真的被释放——
    只删 ref 不 gc,水位永远下不来(注入靶)。
G122 砍尾语义:当前分支重写为"基线 + 最近 K 个",refs() 与 label 对齐,
    砍尾后 restore 仍可用、mark 的空批次缓存(tree↔ref 配对)不指向已 gc 的旧 SHA。

需要真实 git(开发依赖,不是网络):git 缺失时整文件 skip——
**"测不了"和"通过了"必须分开**,否则这一层就变成名义边界。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sigma.cli import migrate_legacy_shadow_dir, shadow_git_dir_for
from sigma_agent.checkpoint import ShadowCheckpoint

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="本机没有 git,checkpoint 用例未验证"
)

TINY_WATERMARK = 100  # 字节——任何真库都超过,治理必然触发


def _cp(root: Path, ws: Path, branch: str = "s1", **over: object) -> ShadowCheckpoint:
    return ShadowCheckpoint(root=root, workspace=ws, branch=branch, **over)  # type: ignore[arg-type]


def _object_count(root: Path) -> int:
    """库里对象总数(loose + pack)——"对象真的被释放"看它,不看 ref 数。"""
    out = subprocess.run(
        ["git", "--git-dir", str(root), "count-objects", "-v"],
        capture_output=True,
        text=True,
        check=True,
    )
    fields = {
        line.split(":")[0]: int(line.split(":")[1].strip())
        for line in out.stdout.strip().splitlines()
        if ":" in line
    }
    return fields["count"] + fields["in-pack"]


# ---------------------------------------------------------------------------
# G119:旧库迁移
# ---------------------------------------------------------------------------


def test_g119_migration_preserves_checkpoints(tmp_path: Path) -> None:
    """旧路径库 → 新路径:rename 而非重建,回滚点一个不少,且幂等。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    legacy = ws / ".sigma" / "shadow.git"
    cp = _cp(legacy, ws)
    for i in range(3):
        (ws / "f.txt").write_text(f"v{i}", encoding="utf-8")
        assert cp.mark(label=f"b{i}")
    before = [c.label for c in cp.refs()]

    assert migrate_legacy_shadow_dir(ws) is True
    assert not legacy.exists()
    assert shadow_git_dir_for(ws).is_dir()

    cp_new = _cp(shadow_git_dir_for(ws), ws)
    assert [c.label for c in cp_new.refs()] == before
    # 幂等:第二次是 no-op(新库已存在)
    assert migrate_legacy_shadow_dir(ws) is False


def test_g119_migration_noop_cases(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # 没有旧库:no-op
    assert migrate_legacy_shadow_dir(ws) is False
    # 新库已存在(比如已经启动过一次):旧库残留也不许覆盖新库
    legacy = ws / ".sigma" / "shadow.git"
    legacy.mkdir(parents=True)
    _cp(shadow_git_dir_for(ws), ws, branch="s1").mark(label="new-lib")
    assert migrate_legacy_shadow_dir(ws) is False
    assert shadow_git_dir_for(ws).is_dir()


# ---------------------------------------------------------------------------
# G120:水位触发与清理梯子
# ---------------------------------------------------------------------------


def test_g120_drops_stale_other_branches_keeps_current(tmp_path: Path) -> None:
    """超限:闲置的旧分支被删,当前会话分支分毫不动。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    stale = _cp(root, ws, branch="old-session", watermark_min_idle_s=0.0)
    (ws / "old.txt").write_text("x" * 5000, encoding="utf-8")
    assert stale.mark(label="old-baseline")
    current = _cp(root, ws, branch="s1", watermark_bytes=TINY_WATERMARK,
                  watermark_min_idle_s=0.0)
    (ws / "cur.txt").write_text("y", encoding="utf-8")
    assert current.mark(label="cur-baseline")

    size = current.enforce_watermark()
    assert size is not None
    branches = subprocess.run(
        ["git", "--git-dir", str(root), "for-each-ref", "--format=%(refname:short)",
         "refs/heads"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert branches == ["s1"], branches
    assert len(current.refs()) == 1


def test_g120_idle_guard_protects_active_sessions(tmp_path: Path) -> None:
    """R4:别的会话最后提交距今不足 min_idle → 不许删(可能是正在跑的会话)。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    fresh = _cp(root, ws, branch="fresh-session")  # min_idle 默认 1h
    (ws / "fresh.txt").write_text("x" * 5000, encoding="utf-8")
    assert fresh.mark(label="fresh-baseline")
    current = _cp(root, ws, branch="s1", watermark_bytes=TINY_WATERMARK)
    (ws / "cur.txt").write_text("y", encoding="utf-8")
    assert current.mark(label="cur-baseline")

    current.enforce_watermark()
    branches = subprocess.run(
        ["git", "--git-dir", str(root), "for-each-ref", "--format=%(refname:short)",
         "refs/heads"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "fresh-session" in branches
    # 清不动就要说出来:清完仍超限的事实留在 last_error,绝不静默
    assert "水位" in current.last_error


# ---------------------------------------------------------------------------
# G121/G122:砍尾、空间回落与砍尾后的一致性
# ---------------------------------------------------------------------------


def test_g121_g122_truncate_frees_objects_and_stays_consistent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    cp = _cp(root, ws, branch="s1", watermark_bytes=TINY_WATERMARK,
             watermark_min_idle_s=0.0)
    for i in range(30):
        (ws / "f.txt").write_text(f"v{i}" + "z" * 400, encoding="utf-8")
        assert cp.mark(label=f"b{i}")
    objects_before = _object_count(root)
    size_before = cp._dir_bytes()  # noqa: SLF001 — 治理信号的测试读数

    assert cp.enforce_watermark() is not None

    objects_after = _object_count(root)
    size_after = cp._dir_bytes()  # noqa: SLF001
    # G121:30 快照 ×3 对象(2 commit+tree 重放后只保留 21×3)——对象必须真的变少
    assert objects_after < objects_before, (objects_before, objects_after)
    assert size_after < size_before

    # G122:refs = 基线 + 最近 20 个,顺序新 → 旧,基线在最底
    refs = cp.refs()
    labels = [c.label for c in refs]
    assert len(refs) == 21
    assert labels[0] == "b29" and labels[1] == "b28" and labels[-2] == "b10"
    assert labels[-1] == "b0"  # 基线(第一个提交)原样保留

    # 砍尾后的 mark 缓存一致性:内容无变化 → 返回**现存** tip
    # (G122 注入靶:_last_ref 不同步时空批次会返回已被 gc 的旧 SHA)
    assert cp.mark(label="no-change") == refs[0].ref

    # 砍尾后 restore 仍可用
    report = cp.restore(refs[5].ref)
    assert report.ok, report.note


def test_g122_mark_cadence_hook_runs_automatically(tmp_path: Path) -> None:
    """mark 每 N 次的节奏钩子:不显式调 enforce,治理也会发生。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    cp = _cp(shadow_git_dir_for(ws), ws, branch="s1",
             watermark_bytes=TINY_WATERMARK, watermark_scan_interval=1,
             watermark_min_idle_s=0.0)
    (ws / "f.txt").write_text("v0", encoding="utf-8")
    assert cp.mark(label="baseline")
    # 第 1 次 mark(count=1,interval=1)就已触发;水位 100 字节必超,
    # 无别的分支可删、快照 ≤ K+1 无从砍 → "仍超"要留在 last_error
    assert "水位" in cp.last_error
