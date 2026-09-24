"""P5 会话切换门槛注入实验：逐条证伪（G87）。

沿用既有框架（在真实仓库上改、跑、finally 还原），不另写 Repo。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E75 | G87 切换后消息落在新会话文件 | ``switch_to`` 的 ``from_store`` 改成空树（只换 id 不换树） | "切完之后说的话"不在新文件、或旧文件被写红 |

**为什么 G87 是最该有的那一条（两条独立的失效路径）**

    ``/switch`` 出错的形状有**两种**，而且它们在 REPL 里看起来一模一样：

    1. **只换 id 不换树**：``SessionTree()`` 而不是 ``from_store(...)``。
       症状是模型突然忘了刚说过的一切——而它照样能答，
       用户会归因成"这次答得不好"。
    2. **只换树不换 ``_session``**：``context`` 还是旧会话的那个。
       症状是**读**对了写错了——切到 B 会话聊了半小时，
       下次 ``--continue`` 找不到那些话（全写进了 A）。

    第 1 种由 ``test_switch_by_index_makes_history_visible`` 钉住（``build_messages``
    里必须出现旧内容），第 2 种由 ``test_later_send_lands_in_the_switched_session_file``
    钉住（新旧文件的**字节**）。**两条一起才叫覆盖**——只钉一条的话，
    另一种失效方式会从缝里漏过去，而漏过去的那一种更难发现。

    E75 打的是第 1 种（``from_store`` 退回空树）：因为"新说的话"要落到
    **新 id** 的文件上这件事由 ``_binding`` 保证，`from_store` 被换成空树后
    那个文件照样被写——红的正是"历史没接上"那半边。
    E76 打的是第 2 种（``_build`` 不更新 ``self._session``）：
    文件还是新建的，但**写进的是旧会话**——新文件不增长、旧文件多一行。

用法
    export PYTHONPATH=scripts
    ./.venv/Scripts/python.exe scripts/gate_injection_p5.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

CLI = "core/sigma/cli.py"

HISTORY_TEST = "tests/test_sigma_repl.py::test_switch_by_index_makes_history_visible"
LANDING_TEST = "tests/test_sigma_repl.py::test_later_send_lands_in_the_switched_session_file"


def _inject_e75(repo: Repo) -> None:
    """E75 / G87-a：``switch_to`` 只换 id 不换树。

    把 ``SessionTree.from_store(...)`` 退回 ``SessionTree()``——
    这正是"实现切换时最容易漏的一步"。漏了的后果**不会报错**：
    会话对象照样建得出来、消息照样能收发，只是模型看不到历史。

    靶测试断言的是 ``build_messages()`` 里有旧会话的内容——
    空树下那里只有系统消息，必然红。
    """
    repo.patch(
        CLI,
        "            tree=SessionTree.from_store(JsonlStore(self._sessions_root, safe)),",
        "            tree=SessionTree(),  # 注入：只换 id 不换树",
    )


def _inject_e76(repo: Repo) -> None:
    """E76 / G87-b：``switch_to`` 换了 ``_binding`` 却忘了换 ``_session``。

    这是"两个状态只改了一个"的经典形状——而它比 E75 更隐蔽：
    内存里 ``current_id`` 已经是新会话了，``/sessions`` 也照新会话显示，
    **只有"新说的话写到哪个文件"这件事错了**。用户切过去聊了半天，
    下次 ``--continue`` 找不到——那时已经没线索了。

    靶测试逐字节比对旧文件，所以"旧文件多了一行"会立刻红。
    """
    repo.patch(
        CLI,
        "        self._binding = binding\n"
        "        self._session = self._build(binding, self._shadow_dir_for(safe))\n"
        "        return SwitchOutcome(ok=True, session_id=safe, messages=messages, switched=switched)",
        "        self._binding = binding\n"
        "        # 注入：忘了换 _session（只改了一个状态）\n"
        "        return SwitchOutcome(ok=True, session_id=safe, messages=messages, switched=switched)",
    )


def main() -> int:
    experiment("G87", "切换后历史真的接上（不是只换 id）", HISTORY_TEST, _inject_e75)
    experiment("G87", "切换后消息落在新会话文件（不是只改内存）", LANDING_TEST, _inject_e76)

    print()
    print("=" * 78)
    print("P5 会话切换门槛注入实验结果")
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
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
