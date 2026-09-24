"""EvalProfile 与 todo/task 门槛注入实验：逐条证伪（G80–G86）。

沿用既有框架（在真实仓库上改、跑、finally 还原），不另写 Repo。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E68 | G80 显式关断压过默认替换 | sdk 的关断分支改成 ``if False:`` | "compaction_policy is None"红 |
| E69 | G81 档位声明不是名义开关 | ``EvalProfile.b1`` 返回全开档 | b1 的 flags 断言红 |
| E70 | G82 至多一条 running | todo 工具的冲突检查改成 ``if False:`` | "第二条 running 被拒"红 |
| E71 | G83 steering 注入真的会发生 | loop 的注入分支改成 ``if True:``（恒 return []） | "第 4 轮前注入提醒"红 |
| E72 | G84 子 registry 无 task（递归禁止） | 克隆排除改成只排 todo | 端到端"子工具集无 task"红 |
| E73 | G85 超长回报截断且可见 | 截断分支改成 ``if False:`` | "已截断"标记 + 长度上限双红 |
| E74 | G86 收尾兜底等在跑的子任务 | loop 兜底的 wait 条件改成 ``if False`` | "慢子任务的回报在 produced 里"红 |

**为什么 G80 的注入"确定性会红"**

    被注入的测试同时给了"必然触发"的策略（窗口 2000、ratio 0）并断言
    ``session.compaction_policy is None``。注入后 ``enable_compaction=False``
    被忽略 → 走 ``elif compaction_policy is not None`` 分支 → 策略被保留 →
    断言立即失败。**不依赖文件系统顺序、不依赖路径长度、不依赖环境。**

**为什么需要 G81**

    档位声明（``EvalProfile``）最大的风险是变成**名义开关**：名字叫 B1、
    字段全开——报告里每一行都写着 B1，实际跑的是 B2。那条红很便宜，
    但它钉住的是"名字必须等于配置"这件事本身。

**为什么需要 G82**

    "同一时刻至多一条 running"是星辰需求「按计划**依次**执行」的机器化。
    删掉它不会崩：两条 running 各自都能 update，清单照常读写——
    只有"依次性"悄悄消失，评测里的长任务变成并行乱序。静默失效是最贵的一种。

**为什么需要 G83**

    steering 的全部价值在"提醒真的会出现"。删掉注入分支后 loop 照常跑、
    计数照常加——只是永远不提醒，防跑偏闸变成摆设。测试断言
    "第 4 轮前 produced 里必须有提醒"，注入后立即红。

**为什么需要 G84**

    「子 agent 不得再派发」是构造上排除（克隆时跳过），没有任何运行期
    报错会提醒"排除丢了"。删掉排除后子 agent 悄悄拿到 task——深度不设限，
    并发 3 的 token 闸只算一层，嵌套派发全部绕过它。唯一能抓它的地方
    是端到端取证：子请求的工具集里不该出现 task。

**为什么需要 G85**

    子任务回报是唯一一条"子上下文→主上下文"的通道。截断分支被删后
    4000+ 字符整段涌入——「隔离上下文」这个工具存在的理由被它自己的
    回报架空，且模型不知道内容被剪过（丢弃必须可见）。双断言（标记 +
    长度上限）保证删分支或改上限都会红。

**为什么需要 G86**

    收尾兜底钉的是 run_turn 的结束条件："模型不再调工具 **且** 信箱排空
    **且** 无在跑子任务"。删掉 wait 之后慢子任务的回报永远留在信箱里
    ——不报错、没有丢失感，主 agent 从此不知道子任务干了什么。
    靶测试专门让回报**不在**最后一轮（慢任务），走的正是兜底路径。

用法
    export PYTHONPATH=scripts
    ./.venv/Scripts/python.exe scripts/gate_injection_eval.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

SDK = "core/sigma/sdk.py"
PROVIDER = "core/sigma_ai/openai/provider.py"
PROFILE = "core/sigma/eval_profile.py"
TODO = "core/sigma_tools/todo.py"
LOOP = "core/sigma_agent/loop.py"
TASK = "core/sigma_tools/task.py"

OFF_TEST = "tests/test_eval_profile.py::test_interactive_session_explicit_compaction_off"
B1_TEST = "tests/test_eval_profile.py::test_b1_disables_every_intervention"
RUNNING_TEST = "tests/test_todo_tool.py::test_update_rejects_second_running"
STEER_TEST = "tests/test_todo_steering.py::test_reminder_injected_after_interval"
SUB_E2E_TEST = "tests/test_sub_agent.py::test_dispatch_runs_sub_session_and_reports_back"
TRUNCATE_TEST = "tests/test_task_tool.py::test_result_truncated_with_visible_marker"
TAILWAIT_TEST = "tests/test_task_tool.py::test_loop_waits_for_pending_subtasks_before_finishing"
TOTAL_TO_TEST = "tests/test_sigma_ai_openai_compat.py::test_total_timeout_guards_slow_drip"
IDLE_TO_TEST = "tests/test_sigma_ai_openai_compat.py::test_idle_timeout_cuts_a_stalled_stream"
BUDGET_TEST = "tests/test_task_tool.py::test_difficulty_selects_round_budget"
REVISE_TEST = "tests/test_todo_tool.py::test_revise_replaces_only_pending"


def _inject_e68(repo: Repo) -> None:
    """E68 / G80：显式关断被忽略。

    后果：B1 档"名存实亡"——评测报告里写着 B1，实际跑的是带压缩（或带
    调用方策略）的配置。对照实验从此测的不是"无干预"，而是"另一种干预"，
    **而报告上完全看不出来**。这类缺陷不会崩、不会报错，只会让结论作废。
    """
    repo.patch(
        SDK,
        "        if not enable_compaction:\n            self._compaction_policy = None",
        "        if False:  # 注入：显式关断被忽略\n"
        "            self._compaction_policy = None",
    )


def _inject_e69(repo: Repo) -> None:
    """E69 / G81：档位声明与配置脱钩。

    ``b1()`` 名义上关掉干预、实际全开——这就是"名义开关"的形状。
    """
    repo.patch(
        PROFILE,
        '        return cls(name="B1", compaction=False, checkpoint=False)',
        '        return cls(name="B1")  # 注入：名字叫 B1，配置是全开',
    )


def _inject_e70(repo: Repo) -> None:
    """E70 / G82：「至多一条 running」检查被删。

    后果：清单照常读写、不崩不报错，但"依次执行"静默消失——
    长任务评测里两条任务并行乱序，trace 的可解释性没了。
    """
    repo.patch(
        TODO,
        "            if params.status == \"running\":",
        "            if False:  # 注入：至多一条 running 的检查被删",
    )


def _inject_e71(repo: Repo) -> None:
    """E71 / G83：steering 注入被删（恒 return []）。

    后果：计数照常加、loop 照常跑，但提醒永远不出现——
    防跑偏闸变成摆设，且没有任何报错。
    """
    repo.patch(
        LOOP,
        "        if self._todo_steer_interval <= 0 or self._todo_stall < self._todo_steer_interval:\n            return []",
        "        if True:  # 注入：steering 注入被删\n            return []",
    )


def _inject_e72(repo: Repo) -> None:
    """E72 / G84：子 registry 克隆不再排除 task（递归禁止失效）。

    后果：子 agent 拿到 task 工具后可以再派子 agent——深度不设限，
    token 成本闸（并发 3）只算一层，嵌套派发全部绕过它。这正是
    星辰需求「工具中不允许包含 task」要防的事。
    只去掉 task 的排除、保留 todo 的排除（否则 DuplicateToolError
    会以脏方式红——报错红不是断言红，取证价值低）。
    """
    repo.patch(
        SDK,
        '                if name in ("task", "todo"):',
        "                if name in (\"todo\",):  # 注入：task 的排除被删",
    )


def _inject_e73(repo: Repo) -> None:
    """E73 / G85：超长回报的截断分支被删。

    后果：子任务回了一个 4000+ 字符的"总结"时，主上下文被整段塞满——
    「隔离上下文」这个 task 工具存在的理由被它自己的回报架空。
    截断标记消失还意味着模型不知道内容被剪过（丢弃必须可见）。
    """
    repo.patch(
        TASK,
        "            if len(text) > MAX_RESULT_CHARS:",
        "            if False:  # 注入：截断分支被删",
    )


def _inject_e74(repo: Repo) -> None:
    """E74 / G86：收尾兜底不再等在跑的子任务。

    后果：模型 dispatch 完最后一个工具就输出文本收尾时，慢子任务的
    结果永远留在信箱里——不报错、不丢失感、没有任何一步红，
    只有"主 agent 从此不知道子任务干了什么"。静默失效的典型形状。
    """
    repo.patch(
        LOOP,
        "        if not msgs and self._mailbox_wait is not None:",
        "        if False and self._mailbox_wait is not None:  # 注入：等完成被删",
    )


def _inject_e75(repo: Repo) -> None:
    """E75 / G87：流式响应的**整体时长闸**被删。

    后果正是 2026-09-23 那次事故：一个连接"一直有心跳但不出内容"，
    httpx 的读超时每次都被心跳重置 → 挂死 1 小时零产出，而同一时刻
    其他连接每轮 4 s 完成。**一条卡死会让整轮评测无限挂起。**
    """
    repo.patch(
        PROVIDER,
        "                    if elapsed_s > self._total_timeout_s:",
        "                    if False:  # 注入：整体时长闸被删",
    )


def _inject_e76(repo: Repo) -> None:
    """E76 / G88：流式响应的**空闲闸**被删（wait_for 不设上限）。

    与 G87 互补：G87 防"慢速滴答"，本条防"彻底停住"。少任何一个，
    都还有一种挂起形状能绕过。
    """
    repo.patch(
        PROVIDER,
        "                            timeout=self._idle_timeout_s,",
        "                            timeout=None,  # 注入：空闲闸被删",
    )


def _inject_e77(repo: Repo) -> None:
    """E77 / G89：难度档位被绕过（一律按 high 给预算）。

    后果：low 档名存实亡——"省 token"的预算设计变成名义开关，
    模型以为派了个便宜的子任务，实际拿到 30 轮预算。这类缺陷不报错，
    只在月底账单上显形。
    """
    repo.patch(
        TASK,
        "            return table[level]",
        "            return self.high  # 注入：档位表被绕过，一律 high",
    )


def _inject_e78(repo: Repo) -> None:
    """E78 / G90：``revise`` 不再保护已完成与正在跑的条目。

    后果：模型中途重排计划时，把**已经做完的记录**和**正在做的那条**一起换掉
    ——历史被伪造、"至多一条 running"的语义也被破坏（正在跑的那条凭空消失）。
    这类缺陷不报错：清单照常读写，只是"事实"变成了"当前想法的投影"。
    """
    repo.patch(
        TODO,
        '        kept = [it for it in data.items if it["status"] != "pending"]',
        '        kept = []  # 注入：已完成与正在跑的也不再保留',
    )


def main() -> int:
    experiment("G80", "显式关断压过默认替换与显式策略", OFF_TEST, _inject_e68)
    experiment("G81", "档位声明不是名义开关（b1 必须真关）", B1_TEST, _inject_e69)
    experiment("G82", "至多一条 running（依次执行的机器化）", RUNNING_TEST, _inject_e70)
    experiment("G83", "steering 提醒真的会发生", STEER_TEST, _inject_e71)
    experiment("G84", "子 registry 无 task（递归禁止=构造上排除）", SUB_E2E_TEST, _inject_e72)
    experiment("G85", "超长回报截断且截断可见", TRUNCATE_TEST, _inject_e73)
    experiment("G86", "收尾兜底等在跑的子任务（结果不丢）", TAILWAIT_TEST, _inject_e74)
    experiment("G87", "流式整体时长闸（防慢速滴答挂死）", TOTAL_TO_TEST, _inject_e75)
    experiment("G88", "流式空闲闸（防彻底停住挂死）", IDLE_TO_TEST, _inject_e76)
    experiment("G89", "难度档位真的决定轮数预算", BUDGET_TEST, _inject_e77)
    experiment("G90", "revise 只换 pending（不动已完成/正在跑）", REVISE_TEST, _inject_e78)

    print()
    print("=" * 78)
    print("EvalProfile 门槛注入实验结果")
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
