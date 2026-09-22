"""批次 13 门槛注入实验：逐条证伪 G64–G70（P3-批次1：L1 写路径 + L2 影子 checkpoint）。

沿用批次 2–4 的框架（在真实仓库上改、跑、finally 还原），**不另写一份 Repo**。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E54 | G64 L1 拦截越界写 | 关掉 containment 检查 | 工作区外的文件真被写出来 |
| E55 | G64b 符号链接逃逸 | 用未 resolve 的路径比较 | 链接指向的外部目录真被写进去 |
| E56 | G65 启动基线 | 不打 baseline | 会话起来后一个快照都没有 |
| E57 | G66 写批次前打快照 | 写批次前不 mark | 写完之后没有任何"写之前"的点 |
| E58 | G67 回滚恢复修改 | reset 只动索引（--mixed） | 改坏的文件内容没回来 |
| E59 | G68 回滚删除新增文件 | 改成 `checkout <ref> -- .` | 新增文件残留（架构 6.2 点名的坑）|
| E60 | G69 不碰用户 .git | 改用工作区自己的 .git | 用户仓库里多出 checkpoint 提交 |
| E61 | G70 快照失败不阻断 | 去掉 loop 侧的兜底 | 快照一抛异常，整轮任务就崩 |

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch13.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

PATHS = "core/sigma_tools/_paths.py"
LOOP = "core/sigma_agent/loop.py"
CHECKPOINT = "core/sigma_agent/checkpoint.py"
SDK = "core/sigma/sdk.py"

L1_TESTS = "tests/test_tools_paths_l1.py"
LOOP_TESTS = "tests/test_agent_loop.py"
CP_TESTS = "tests/test_agent_checkpoint.py"
CLI_TESTS = "tests/test_sigma_cli_session.py"


def _inject_e54(repo: Repo) -> None:
    """E54 / G64：不做边界检查（"顺手不写这一句"就是这个样子）。"""
    repo.patch(
        PATHS,
        "    if not _same_or_inside(resolved, root):",
        "    if False:  # 注入：不做边界检查",
    )


def _inject_e55(repo: Repo) -> None:
    """E55 / G64b：不解析符号链接——文本层面"在界内"，实际写到外面。"""
    repo.patch(
        PATHS,
        "    resolved = candidate.resolve(strict=False)",
        "    resolved = candidate  # 注入：不解析符号链接",
    )


def _inject_e56(repo: Repo) -> None:
    """E56 / G65：会话起来时不打基线。"""
    repo.patch(
        SDK,
        '            self._checkpoint.mark(label="baseline")',
        '            pass  # 注入：不打启动基线',
    )


def _inject_e57(repo: Repo) -> None:
    """E57 / G66：写批次前不打快照——"可回滚"退化成"什么都回不去"。"""
    repo.patch(
        LOOP,
        "        try:\n            self._checkpoint.mark(label=f\"write-batch:{names}\")",
        "        if False:  # 注入：写批次前不打快照\n"
        "            self._checkpoint.mark(label=f\"write-batch:{names}\")",
    )


def _inject_e58(repo: Repo) -> None:
    """E58 / G67：reset 只重置索引，工作区一个字节不动。"""
    repo.patch(
        CHECKPOINT,
        '        reset = self._git("reset", "--hard", "--quiet", ref)',
        '        reset = self._git("reset", "--mixed", "--quiet", ref)  # 注入：只重置索引',
    )


def _inject_e59(repo: Repo) -> None:
    """E59 / G68：只 checkout——**架构 6.2 点名的坑**，新增文件不会被删。"""
    repo.patch(
        CHECKPOINT,
        '        reset = self._git("reset", "--hard", "--quiet", ref)',
        '        reset = self._git("checkout", ref, "--", ".")  # 注入：只 checkout',
    )


def _inject_e60(repo: Repo) -> None:
    """E60 / G69：影子库指到工作区自己的 .git——污染用户真实历史。"""
    repo.patch(
        CHECKPOINT,
        '                "--git-dir",\n                str(self._root),',
        '                "--git-dir",\n'
        '                str(self._workspace / ".git"),  # 注入：污染用户仓库',
    )


def _inject_e61(repo: Repo) -> None:
    """E61 / G70：去掉 loop 侧的兜底——快照抛异常就把任务带崩。"""
    repo.patch(
        LOOP,
        '        try:\n            self._checkpoint.mark(label=f"write-batch:{names}")\n'
        "        except Exception:",
        '        self._checkpoint.mark(label=f"write-batch:{names}")\n'
        "        if False:  # 注入：不兜底",
    )


def _inject_e62(repo: Repo) -> None:
    """E62 / G71：去掉工作区配对闸门。

    这一条的注入后果是**灾难性**的：影子库给 A 建、却拿 B 去 restore，
    `reset --hard` 会把 B 里"A 快照没有"的文件全删掉。
    2026-09-22 的真模型冒烟真的踩过（删了仓库 171 个文件）。
    """
    repo.patch(
        CHECKPOINT,
        "        mismatch = self.workspace_mismatch()\n"
        "        if mismatch:\n"
        "            return RestoreReport(ok=False, ref=ref, note=mismatch)",
        "        mismatch = \"\"  # 注入：不做工作区配对检查\n"
        "        if mismatch:\n"
        "            return RestoreReport(ok=False, ref=ref, note=mismatch)",
    )


def main() -> int:
    experiment("G64", "L1：工作区外的写被拒绝", L1_TESTS, _inject_e54)
    experiment("G64b", "L1：符号链接逃逸被拒绝", L1_TESTS, _inject_e55)
    experiment("G65", "会话启动打基线快照", CLI_TESTS, _inject_e56)
    experiment("G66", "写批次前打快照", LOOP_TESTS, _inject_e57)
    experiment("G67", "回滚恢复被改的文件", CP_TESTS, _inject_e58)
    experiment("G68", "回滚删除新增文件（6.2 的坑）", CP_TESTS, _inject_e59)
    experiment("G69", "不碰用户仓库的 .git", CP_TESTS, _inject_e60)
    experiment("G70", "快照失败不阻断任务", LOOP_TESTS, _inject_e61)
    experiment("G71", "回滚前核对工作区配对（防灾难性回滚）", CP_TESTS, _inject_e62)

    print()
    print("=" * 78)
    ok = True
    for gate, what, passed, detail in RESULTS:
        flag = "PASS" if passed else "FAIL"
        ok = ok and passed
        print(f"[{flag}] {gate}  {what}")
        if not passed:
            print(f"        {detail}")
    proven = sum(1 for row in RESULTS if row[2])
    print("=" * 78)
    print(f"共 {len(RESULTS)} 条注入实验，{proven} 条被成功证伪")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
