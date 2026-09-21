"""P2-2 门槛注入实验：逐条证伪会话树门槛（G48 / G49 / G53）。

沿用批次 2–4 / 7 / 8 / P2-1 的框架（在真实仓库上改、跑、finally 还原），
**不另写一份 Repo**——验证工具自身会腐化，两份实现意味着修一个忘另一个。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E59 | G48 树路径只含本分支 | `path_to` 改成返回全部节点 | 分支测试里出现兄弟节点 |
| E60 | G49 环检测要有**具体链条** | 关掉 `seen` 诊断（只留步数上限）| 原因里没有"成环"与具体链 |
| E61 | G53 `load()` 是替换语义 | `=` 改成 `+=`（累加）| 重载后节点数翻倍 |

**E60 为什么断言"具体链条"而不是只断言"节点被判坏"**

    本实现有**两条防线**：``seen`` 集合给精确诊断，步数上限保证不挂死。
    只关掉防线一时，防线二仍然会把节点判成"坏"——
    于是"节点坏了"这条断言**依然会绿**，得到一条假绿。

    断言必须落在**只有防线一才能提供的信息**上（具体环链 "A -> B -> A"），
    这样防线一失效才会红。**兜底不等于正确**，这条门槛就是为这句话设的。

    顺带：这也避免了注入后**挂死**——若没有防线二，关掉 `seen`
    会让回溯无限循环，测试进程永远不返回。挂死的 CI 比失败的 CI 难排查得多，
    所以防线二既是工程保险，也是让这条注入实验可执行的前提。

**G53 为什么是"修订版"**

    计划原文是"同一批消息重复 ``append`` 后不产生重复节点"，
    注入方式是"去掉去重/幂等键"。**实际施工时发现那个前提不成立**：

    - 每条记录 id 是 uuid4，重复 ``append`` 本就该产生**两个不同节点**
      （它们是两条不同消息），去重会造成**静默的数据丢失**（用户连说两次"好"丢一句）；
    - 设计里根本没有"去重键"，无从注入。

    所以门槛换成真实存在的相邻属性：``load()`` 是**替换**而不是**追加**。
    它的失败模式实在（每次重载节点数翻倍，且不报错）。
    **不为一个不存在的需求引入一个真缺陷。**

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch9.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

TREE = "core/sigma_session/tree.py"

TREE_TESTS = "tests/test_session_tree.py"

BRANCH_TEST = f"{TREE_TESTS}::test_path_to_contains_only_own_branch"
CYCLE_TEST = f"{TREE_TESTS}::test_cycle_is_detected_with_specific_chain"
RELOAD_TEST = f"{TREE_TESTS}::test_reload_does_not_accumulate"


def _inject_e59(repo: Repo) -> None:
    """E59 / G48：丢掉分支隔离——``path_to`` 返回**全部**节点。

    这是"图省事"的形态：既然要"完整历史"，就把所有节点都给它。
    症状是**模型看到别的分支说过的话**，在长会话里表现为
    "它提到了我没说过的内容"，而没有任何报错。
    """
    repo.patch(
        TREE,
        "        ids.reverse()\n        return ids",
        "        ids = [r.id for r in self._raw]  # 注入：丢掉分支隔离\n"
        "        return ids",
    )


def _inject_e60(repo: Repo) -> None:
    """E60 / G49：关掉 ``seen`` 环诊断，只留步数上限。

    注入后节点**仍然会被判成坏**（防线二兜住），所以"节点坏了"的断言会假绿；
    只有断言"诊断里必须给出具体环链"才能让它红。
    """
    repo.patch(
        TREE,
        "            if cursor in index:\n"
        "                # 防线一：给出**具体的环**，这是诊断质量所在\n"
        "                members = path[index[cursor] :]",
        "            if False:  # 注入：关掉精确环诊断\n"
        "                members = path[index[cursor] :]",
    )


def _inject_e61(repo: Repo) -> None:
    """E61 / G53（修订版）：``load()`` 从"替换"改成"累加"。

    症状是每次重载节点数**翻倍**、``roots()`` 出现重复根——
    而进程不报错，只是"历史越来越长"。这类缺陷在不重启的长驻进程里
    会一路涨到上下文预算炸掉才被发现。
    """
    repo.patch(
        TREE,
        "        self._raw = list(result.records)",
        "        self._raw += list(result.records)  # 注入：累加而不是替换",
    )


def main() -> int:
    experiment("G48", "树路径只含本分支（不混入兄弟）", BRANCH_TEST, _inject_e59)
    experiment("G49", "环检测必须给出具体链条", CYCLE_TEST, _inject_e60)
    experiment("G53", "load() 是替换语义（重载不累加）", RELOAD_TEST, _inject_e61)

    print()
    print("=" * 78)
    print("P2-2 会话树门槛注入实验结果")
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
    print("注：G53 用的是**修订版**（load 替换语义），不是计划原文的")
    print("    「重复 append 去重」——原文前提不成立，理由见本文件 docstring。")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
