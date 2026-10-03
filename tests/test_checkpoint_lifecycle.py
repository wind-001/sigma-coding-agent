"""影子库生命周期回收的测试(2026-10-03)。

设计动机:水位治理只治"总量尖峰",低于水位时分支与对象只进不出——
这是"文件债"的稳态来源。回收改成生命周期触发后,这些不变量必须被钉住:

R1 正在跑的不能动:闲置不足 min_idle 的分支可能是并行会话,任何路径不删;
R2 最近关掉的留一份:非当前、已结束的分支里,最新的 keep 个保留;
R3 再早的过期作废:闲置超过 TTL 的分支,连"最近一份"的资格也没有;
R4 当前分支任何路径都不删;
R5 gc --prune=now 失败必须留下欠条,清扫重试成功后销账——
   孤儿对象不能因为一次 gc 失败就永生(实测过 24MB 零分支的库);
R6 崩溃残留的 index.lock 被摘,活锁(新近的)不动;
R7 懒基线不变量:库还不存在时,清扫/收尾是无害 no-op,**不许凭空造库**。

committerdate 只有秒级粒度,"谁更新"需要区分先后的用例里 sleep 一秒——
比引入可控时钟的复杂度便宜,也符合本文件"要真实 git"的姿态。

需要真实 git(开发依赖,不是网络):git 缺失时整文件 skip。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from sigma.cli.main import shadow_git_dir_for
from sigma.security.shadow_checkpoint import (
    NEEDS_PRUNE_MARKER,
    STALE_LOCK_MIN_AGE_S,
    ShadowCheckpoint,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="本机没有 git,checkpoint 用例未验证"
)


def _branches(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "--git-dir", str(root), "for-each-ref",
         "--format=%(refname:short)", "refs/heads"],
        capture_output=True, text=True, check=True,
    )
    return sorted(out.stdout.split())


def _object_count(root: Path) -> int:
    """库里对象总数(loose + pack)——"对象真的被释放"看它,不看 ref 数。"""
    out = subprocess.run(
        ["git", "--git-dir", str(root), "count-objects", "-v"],
        capture_output=True, text=True, check=True,
    )
    fields = {
        line.split(":")[0]: int(line.split(":")[1].strip())
        for line in out.stdout.strip().splitlines()
        if ":" in line
    }
    return fields["count"] + fields["in-pack"]


def _session(root: Path, ws: Path, branch: str, **over: object) -> ShadowCheckpoint:
    cp = ShadowCheckpoint(
        root=root, workspace=ws, branch=branch, **over  # type: ignore[arg-type]
    )
    (ws / f"{branch}.txt").write_text(branch * 64, encoding="utf-8")
    assert cp.mark(label=f"{branch}-baseline")
    return cp


# ---------------------------------------------------------------------------
# retire_ended_branches:R1–R4
# ---------------------------------------------------------------------------


def test_r2_retire_keeps_newest_ended_deletes_rest(tmp_path: Path) -> None:
    """三个已结束分支里只留最新一个;当前分支不动。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    _session(root, ws, "old-a", watermark_min_idle_s=0.0)
    time.sleep(1.1)  # committerdate 秒级粒度:不睡,"谁更新"是抛硬币
    _session(root, ws, "old-b", watermark_min_idle_s=0.0)
    current = _session(root, ws, "s1", watermark_min_idle_s=0.0)

    deleted = current.retire_ended_branches()

    assert deleted == 1
    assert _branches(root) == ["old-b", "s1"]  # old-b 最新,留;old-a 删


def test_r3_ttl_expires_even_the_newest(tmp_path: Path) -> None:
    """闲置超过 TTL 的分支连"最近一份"的资格也没有,且对象真的被释放。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    _session(root, ws, "old", watermark_min_idle_s=0.0)
    current = _session(root, ws, "s1", watermark_min_idle_s=0.0)
    objects_before = _object_count(root)

    deleted = current.retire_ended_branches(ttl_s=0.0)

    assert deleted == 1
    assert _branches(root) == ["s1"]
    assert _object_count(root) < objects_before  # 只删 ref 不 gc = 水位没意义


def test_r4_retire_never_deletes_current_branch(tmp_path: Path) -> None:
    """当前分支就算唯一、就算"超 TTL",也不删——它是本会话的后悔药。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    cp = _session(root, ws, "s1", watermark_min_idle_s=0.0)

    assert cp.retire_ended_branches(ttl_s=0.0) == 0
    assert _branches(root) == ["s1"]


def test_r1_retire_idle_guard_protects_running_session(tmp_path: Path) -> None:
    """闲置不足 min_idle 的分支可能是并行的活会话——TTL 再短也不许碰。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    _session(root, ws, "parallel-live")  # min_idle 默认 1h:刚提交 = 受保护
    current = _session(root, ws, "s1")

    assert current.retire_ended_branches(ttl_s=0.0) == 0
    assert _branches(root) == ["parallel-live", "s1"]


def test_retire_empty_pool_is_noop(tmp_path: Path) -> None:
    """没有别的分支(或库为空):返回 0,无异常。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    cp = _session(root, ws, "s1", watermark_min_idle_s=0.0)
    assert cp.retire_ended_branches() == 0


# ---------------------------------------------------------------------------
# 欠账标记:R5
# ---------------------------------------------------------------------------


def test_r5_gc_failure_leaves_marker_retry_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gc --prune=now 失败 → 欠条落盘;重试成功 → 销账。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    cp = _session(root, ws, "s1")
    real_git = ShadowCheckpoint._git

    def failing_git(self: ShadowCheckpoint, *args: object, **kw: object) -> CompletedProcess:
        result = real_git(self, *args, **kw)  # type: ignore[arg-type]
        if "--prune=now" in args:
            return CompletedProcess(args, 1, "", "boom")  # type: ignore[arg-type]
        return result  # type: ignore[return-value]

    monkeypatch.setattr(ShadowCheckpoint, "_git", failing_git)
    cp._gc_prune()  # noqa: SLF001 — 欠账机制的被测对象
    assert (root / NEEDS_PRUNE_MARKER).exists()

    monkeypatch.setattr(ShadowCheckpoint, "_git", real_git)
    cp._retry_pending_prune()  # noqa: SLF001
    assert not (root / NEEDS_PRUNE_MARKER).exists()


def test_r5_retry_pending_prune_is_noop_without_marker(tmp_path: Path) -> None:
    """没有欠条时重试是无害 no-op,不碰库。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    cp = _session(root, ws, "s1")
    cp._retry_pending_prune()  # noqa: SLF001
    assert not (root / NEEDS_PRUNE_MARKER).exists()
    assert _branches(root) == ["s1"]


# ---------------------------------------------------------------------------
# 启动清扫:R6 + R7
# ---------------------------------------------------------------------------


def test_r6_stale_index_lock_swept_fresh_lock_kept(tmp_path: Path) -> None:
    """超龄 index.lock(崩溃残留)被摘;新近的锁(活 add)不动。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    cp = _session(root, ws, "s1")
    lock = root / "index.lock"

    lock.write_text("", encoding="utf-8")
    old = time.time() - STALE_LOCK_MIN_AGE_S - 60
    os.utime(lock, (old, old))
    cp.startup_sweep()
    assert not lock.exists()

    lock.write_text("", encoding="utf-8")  # 新鲜的锁:可能是并行会话正在 add
    cp.startup_sweep()
    assert lock.exists()


def test_r7_sweep_on_missing_repo_is_noop_and_creates_nothing(tmp_path: Path) -> None:
    """懒基线(G65):没写过文件的会话没有库,清扫不许凭空造一个。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    cp = ShadowCheckpoint(root=root, workspace=ws, branch="s1")

    cp.startup_sweep()
    cp.close_session()
    assert cp.retire_ended_branches() == 0
    assert not root.exists()  # 库还是没被造出来


def test_startup_sweep_reclaims_orphan_objects(tmp_path: Path) -> None:
    """零分支却躺着对象 = 旧账(删分支后 gc 失败的年代),清扫直接还清。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    cp = _session(root, ws, "doomed", watermark_min_idle_s=0.0)
    assert _object_count(root) > 0
    subprocess.run(
        ["git", "--git-dir", str(root), "update-ref", "-d", "refs/heads/doomed"],
        check=True,
    )  # 模拟当年的事故:拔了夹子,gc 却没跑成
    assert _branches(root) == []

    cp.startup_sweep()
    # 空树对象(4b825dc…)是 git 的常驻对象,可能被 gc 留在 pack 里——
    # 40 字节,不随引用消失,也不算债;除此之外的底片必须清零。
    assert _object_count(root) <= 1


# ---------------------------------------------------------------------------
# close_session:组合行为
# ---------------------------------------------------------------------------


def test_close_session_keeps_current_and_recent_ended(tmp_path: Path) -> None:
    """收尾 = gc + 水位 + 回收:当前分支与"最近一份"都在,更早的走人。

    三个会话依次收尾的滚动窗口:s2 收尾时 s1 是"最近一份"被留;
    s3 收尾时 s1 已不是最近,s2 顶上来——任意时刻至多一份已结束分支。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    root = shadow_git_dir_for(ws)
    _session(root, ws, "s1", watermark_min_idle_s=0.0)
    time.sleep(1.1)
    s2 = _session(root, ws, "s2", watermark_min_idle_s=0.0)
    s2.close_session()
    assert _branches(root) == ["s1", "s2"]  # s1 = 最近关掉的,留一份

    time.sleep(1.1)
    s3 = _session(root, ws, "s3", watermark_min_idle_s=0.0)
    s3.close_session()
    assert _branches(root) == ["s2", "s3"]  # s1 过期作废,s2 顶上
