"""回滚成功率统计:纯脚本驱动影子 git checkpoint,不经模型(P5-批次2 波 1,主张 6)。

方法
    固定模式构造 30 次写批次(改/增/删交错),每次 mark 前记录"期望状态"
    (内存模型);全部写完后,对**每一个快照**执行 restore 并逐文件比对
    实际工作区与该时点的期望状态——把 G66 族单测断言换成统计口径。

口径(诚实声明,报告引用)
    纯 git 层:验证"恢复后工作区与快照点完全一致"(7.5 的回滚成功率定义),
    **不含**"模型写完又自己改"的并发竞态,也不含 >5MB/排除清单外的文件
    (那些本来就不参与快照)。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sigma_agent.checkpoint import ShadowCheckpoint

#: 初始文件(全部参与快照:小文本、无 .gitignore 干扰)。
INITIAL_FILES: dict[str, str] = {
    "file0.txt": "init-0",
    "file1.txt": "init-1",
    "file2.txt": "init-2",
    "README.md": "# stats fixture\n",
}


def tree_diff(actual: dict[str, str], expected: dict[str, str]) -> list[str]:
    """两份"相对路径→内容"映射的逐文件比对。返回差异列表(空 = 一致)。

    内容必须比对——只比文件名集合会漏掉"同名不同内容"的静默错位
    (E874 注入实验证的正是这条)。
    """
    diffs: list[str] = []
    for name in sorted(set(actual) | set(expected)):
        if name not in expected:
            diffs.append(f"多出 {name}")
        elif name not in actual:
            diffs.append(f"缺失 {name}")
        elif actual[name] != expected[name]:
            diffs.append(f"内容不一致 {name}")
    return diffs


def _expected_state_after(i: int) -> dict[str, str]:
    """第 i 次写批次(0 起)执行后的期望工作区状态(固定模式,无随机)。"""
    state = dict(INITIAL_FILES)
    for step in range(i + 1):
        state[f"file{step % 4}.txt"] = f"v{step}"
        if step % 3 == 0:
            state[f"extra{step}.txt"] = f"extra-{step}"
        if step % 5 == 4:
            state.pop(f"extra{step - 3}.txt", None)
    return state


def _apply_write(i: int, workspace: Path) -> None:
    """执行第 i 次写批次(与 _expected_state_after 严格同模式)。"""
    (workspace / f"file{i % 4}.txt").write_text(f"v{i}", encoding="utf-8")
    if i % 3 == 0:
        (workspace / f"extra{i}.txt").write_text(f"extra-{i}", encoding="utf-8")
    if i % 5 == 4:
        victim = workspace / f"extra{i - 3}.txt"
        if victim.exists():
            victim.unlink()


def _scan_workspace(workspace: Path) -> dict[str, str]:
    """实际工作区状态(相对路径→文本内容),跳过 .sigma 与子目录里的库。"""
    actual: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        if rel.startswith(".sigma"):
            continue
        try:
            actual[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            actual[rel] = "<unreadable>"
    return actual


@dataclass
class RollbackReport:
    iterations: int = 0
    snapshots_checked: int = 0
    successes: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return not self.failures and self.snapshots_checked == self.iterations


def run_rollback_stats(iterations: int = 30, base_tmp: Path | None = None) -> RollbackReport:
    """跑 N 次写→快照→回滚→逐文件比对的统计。iterations=3 供单测冒烟。"""
    report = RollbackReport(iterations=iterations)
    owned = base_tmp is None
    tmp = (
        Path(tempfile.mkdtemp(prefix="sigma-rollback-"))
        if base_tmp is None
        else base_tmp
    )
    try:
        workspace = tmp / "ws"
        workspace.mkdir(parents=True)
        for name, content in INITIAL_FILES.items():
            (workspace / name).write_text(content, encoding="utf-8")

        checkpoint = ShadowCheckpoint(
            root=tmp / "shadow.git", workspace=workspace, branch="stats-session"
        )
        if not checkpoint.available:
            report.failures.append(f"影子库不可用:{checkpoint.unavailable_reason}")
            return report

        # 写阶段:mark(执行前)→ 写 → 记"mark 时点的期望状态"。
        pre_states: dict[str, dict[str, str]] = {}
        state = dict(INITIAL_FILES)
        for i in range(iterations):
            ref = checkpoint.mark(label=f"write-{i}")
            if ref is None:
                report.failures.append(f"write-{i}: mark 失败:{checkpoint.last_error}")
                return report
            pre_states[f"write-{i}"] = dict(state)
            _apply_write(i, workspace)
            state = _expected_state_after(i)

        # 验阶段:每个快照恢复后与该时点期望状态逐文件比对(含删除场景)。
        # **快照清单在循环前取一次**:每次 restore 会自保一个
        # "pre-restore:*" 快照,循环内再取 refs() 会混进这些验证副产物。
        refs = checkpoint.refs()
        for info in refs:
            report.snapshots_checked += 1
            expected = pre_states.get(info.label)
            if expected is None:
                report.failures.append(f"{info.label}: 无期望状态(标签对不上)")
                continue
            restore = checkpoint.restore(info.ref)
            if not restore.ok:
                report.failures.append(f"{info.label}: restore 失败:{restore.note}")
                continue
            diffs = tree_diff(_scan_workspace(workspace), expected)
            if diffs:
                report.failures.append(f"{info.label}: {'; '.join(diffs[:5])}")
            else:
                report.successes += 1

        # 收尾:回到最新快照并验证(统计过程本身不留破坏)。
        # 两个语义坑(首版都踩过):懒基线——最后快照 = 最后一次写**之前**的
        # 状态,期望取 pre_states;restore 自保快照——refs 必须用循环前那份。
        if refs:
            checkpoint.restore(refs[0].ref)
            diffs = tree_diff(_scan_workspace(workspace), pre_states[refs[0].label])
            if diffs:
                report.failures.append(f"收尾恢复最新态失败:{'; '.join(diffs[:5])}")
    finally:
        if owned:
            shutil.rmtree(tmp, ignore_errors=True)
    return report


def main() -> int:
    report = run_rollback_stats()
    reports = Path(__file__).resolve().parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "rollback_stats.json").write_text(
        json.dumps(
            {
                "iterations": report.iterations,
                "snapshots_checked": report.snapshots_checked,
                "successes": report.successes,
                "failures": report.failures,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"回滚成功率 {report.successes}/{report.snapshots_checked}"
        f"(写批次 {report.iterations} 次)"
    )
    for failure in report.failures:
        print(f"  ✗ {failure}", file=sys.stderr)
    return 0 if report.all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
