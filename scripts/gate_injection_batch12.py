"""P2-5 门槛注入实验：逐条证伪会话接续门槛（G61 / G62 / G63）。

沿用批次 2–4 / 7 / 8 / P2-1…P2-4 的框架（在真实仓库上改、跑、finally 还原），
**不另写一份 Repo**——验证工具自身会腐化，两份实现意味着修一个忘另一个。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E69 | G61 续接真的读回历史 | `SessionTree.from_store` 跳过 `load()` | 重启后历史为空 |
| E70 | G61b `resolve_session` 报告已续接 | 同上（`from_store` 不读盘）| `resumed` 对了但条数是 0 |
| E71 | G62 两个会话不串味 | `JsonlStore.path` 返回固定文件名 | 两个会话并到一个文件 |
| E72 | G63 `--continue` 与 `--session` 互斥 | 互斥组换成普通参数组 | 同时给不再报错 |

**为什么 G61 要点两条注入**

    它的两个断言在不同的层上：``test_resumed_session_sees_previous_history``
    直接构造树（绕过 CLI），验的是"**读盘这一步真的发生了**"；
    ``test_resolve_session_reuses_existing`` 走 ``resolve_session``，
    验的是"**解析层把已存在的会话认出来了**"。

    只注一条的话，另一条永远没被检验过。而这两层里任何一层坏掉，
    症状都是"模型忘了之前说过什么"——**看起来只是这次答得不好**。

**E71 为什么值得单独一条**

    会话 id → 文件名有一次净化（``JsonlStore.path`` 把 ``/`` ``\\`` 换成 ``_``）。
    净化的本意是"别让 ``--session a/b`` 写到 root 之外"，而它一旦退化成
    "所有 id 都映射到同一个文件"，两个会话就**并到一处**——
    你在 A 会话里说的话会出现在 B 会话的上下文里。
    这条没有任何报错，只是上下文里多了一段"我没说过的话"。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch12.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

TREE = "core/sigma_session/tree.py"
STORE = "core/sigma_session/store.py"
CLI = "core/sigma/cli.py"

CLI_TESTS = "tests/test_sigma_cli_session.py"

RESUMED_TEST = f"{CLI_TESTS}::test_resumed_session_sees_previous_history"
RESOLVE_TEST = f"{CLI_TESTS}::test_resolve_session_reuses_existing"
ISOLATION_TEST = f"{CLI_TESTS}::test_two_sessions_get_two_files"
MUTEX_TEST = f"{CLI_TESTS}::test_continue_and_session_are_mutually_exclusive"


def _inject_e69(repo: Repo) -> None:
    """E69 / G61：``from_store`` 不读盘。

    这是"接了个 id，但没把历史读回来"的形态。后果不是报错，
    而是模型**忘了之前的一切**——它会重新问用户已经说过的事，
    或者把刚改好的文件再改一遍，而看起来只是"这次答得不好"。
    """
    repo.patch(
        TREE,
        "        tree = cls(store=store)\n        tree.load()\n        return tree",
        "        tree = cls(store=store)\n"
        "        return tree  # 注入：不读盘（接了个空壳）",
    )


def _inject_e70(repo: Repo) -> None:
    """E70 / G61b：同一条注入，打在**解析层**那条断言上。

    与 E69 是同一次修改、不同的证据路径（见模块 docstring）。
    """
    repo.patch(
        TREE,
        "        tree = cls(store=store)\n        tree.load()\n        return tree",
        "        tree = cls(store=store)\n"
        "        return tree  # 注入：不读盘（接了个空壳）",
    )


def _inject_e71(repo: Repo) -> None:
    """E71 / G62：所有会话映射到同一个文件。

    净化的本意是防止 ``--session a/b`` 写出 root；把它退化成固定文件名后，
    两个会话**并到一处**——历史互相污染，且没有任何报错。
    """
    repo.patch(
        STORE,
        '        return self._root / f"{safe}.jsonl"',
        '        return self._root / "shared.jsonl"  # 注入：所有会话同一个文件',
    )


def _inject_e72(repo: Repo) -> None:
    """E72 / G63：去掉两个开关的互斥。

    去掉之后 ``--continue --session x`` 会被接受，而两者语义冲突
    （一个说"续最近那个"，一个说"用这个 id"）——只能靠"谁先赋值"决定，
    于是行为变成隐式的。**启动时报错比让后者静默覆盖前者好。**
    """
    repo.patch(
        CLI,
        "    session_group = parser.add_mutually_exclusive_group()",
        "    # 注入：去掉互斥\n"
        "    session_group = parser.add_argument_group()",
    )


def main() -> int:
    experiment("G61", "续接真的读回历史（端到端）", RESUMED_TEST, _inject_e69)
    experiment("G61b", "解析层认出已存在的会话", RESOLVE_TEST, _inject_e70)
    experiment("G62", "两个会话不串味（两个文件）", ISOLATION_TEST, _inject_e71)
    experiment("G63", "--continue 与 --session 互斥", MUTEX_TEST, _inject_e72)

    print()
    print("=" * 78)
    print("P2-5 会话接续门槛注入实验结果")
    print("=" * 78)
    ok = 0
    for gate, what, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        if passed:
            ok += 1
        print(f"[{mark}] {gate}  {what}")
        print(f"       → {detail}")
    print("=" * 78)
    print(f"{ok}/{len(RESULTS)} 条门槛被成功证伪（注入后确实变红）")
    print()
    print("注：G61 拆成两条证据路径（端到端 / 解析层），用的是同一次注入。")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
