"""P2-1 门槛注入实验：逐条证伪回放场景门槛（G55–G56、G58）。

沿用批次 2–4 / 7 / 8 的框架（在真实仓库上改、跑、finally 还原），
**不另写一份 Repo**——验证工具自身会腐化，两份实现意味着修一个忘另一个。

**编号为什么从 G55 起**

    `docs/plans/P2-会话树与上下文-详规.md` 第 5 节已经把 G48–G54 分配给
    P2 主体（树路径 / 环检测 / 常驻区稳定 / 压缩降级 / 压缩保真 / 追加幂等 /
    `AGENTS.md` 截断）。P2-1 是**在 P2 主体之前**做的，很容易顺手占用 G48——
    我第一版就占了，然后与计划撞号。**改号而不是改计划**：
    计划里的门槛是按依赖顺序排的，P2-1 是本阶段的**前置件**，
    用计划之后的号段（G55+）更贴合它"先落地、后被主体依赖"的位置。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E54 | G55 单条工具失败不得中止整批 | 恢复"一失败就 return"分支 | tool_error_recovery 轮数掉到 3 |
| E56 | G56 transcript 必须被完整消费 | loop 提前退出（不消费剩余轮次）| 场景报"剩 N 轮" |
| E57 | G57 回放工作区必须隔离（**实测证伪不了 → 未注册**）| 场景共用同一目录 | 全绿——所以它不是门槛 |
| E58 | G58 `TurnResult.usage` 必须累计 | 退回"只取最后一轮" | 两轮任务的 token 只剩一轮 |

**E56 为什么不是循环论证**

    "transcript 被完整消费"这条断言看起来像是回放器自己保证的
    （它当然会消费完，除非 loop 提前退出）。
    但它真正防的是**loop 提前退出**——即"模型还会说话，loop 已经收工"。
    E56 注入的正是这个：让 loop 在还剩输入时结束。
    若门槛真在防它，这条注入必须让场景变红。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_p21.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

LOOP = "core/sigma_agent/loop.py"
SCENARIOS = "tests/fixtures/transcripts/_scenarios.py"
WORKSPACE_FIXTURE = "tests/fixtures/workspace.py"

SCENARIO_TESTS = "tests/test_transcript_scenarios.py"
LOOP_TESTS = "tests/test_agent_loop.py"
SELF_CORRECTION_TEST = (
    f"{SCENARIO_TESTS}::test_tool_error_does_not_abort_the_batch"
)


def _inject_e58(repo: Repo) -> None:
    """E58 / G58：`TurnResult.usage` 退回"只取最后一轮"。

    这正是 2026-09-21 修掉的那个缺陷本身。它当时**全套测试都没抓到**——
    因为没有任何一条断言看过 `result.usage`。
    本注入就是钉住"它现在会被抓到"。

    症状（修之前）：`evals/runner.py` 报告里一个 6 轮任务的 prompt token
    只等于第 6 轮的；"每任务 token"这个指标因此系统性偏低，
    **而没有任何东西会报错**。
    """
    repo.patch(
        LOOP,
        "            total_usage = _add_usage(total_usage, assistant.usage)",
        "            total_usage = assistant.usage  # 注入：只取最后一轮",
    )


def _inject_e54(repo: Repo) -> None:
    """E54 / G55：恢复"一失败就 return"的分支。

    这正是批次 2–4 的 E26 注入过的同一个分支——**在这里再注一次不是重复**：
    E26 用的是手工构造的消息，这里用的是**回放场景**。
    两条证据路径不同：一条证明"loop 逻辑对"，一条证明"场景真能测到它"。
    若只有 E26，场景文件写错了也没人知道。
    """
    repo.patch(
        LOOP,
        "                produced.append(\n"
        "                    ToolResultAgentMessage.from_result(\n"
        "                        item.to_block(), result, timestamp=self._clock()\n"
        "                    )\n"
        "                )",
        "                # 注入：工具一失败就结束整轮（关掉纠错能力）\n"
        "                if result.is_error:\n"
        "                    produced.append(\n"
        "                        ToolResultAgentMessage.from_result(\n"
        "                            item.to_block(), result, timestamp=self._clock()\n"
        "                        )\n"
        "                    )\n"
        "                    return TurnResult(\n"
        '                        status="completed",\n'
        "                        messages=produced,\n"
        "                        text=_text_of(assistant),\n"
        "                        rounds=round_index,\n"
        "                    )\n"
        "                produced.append(\n"
        "                    ToolResultAgentMessage.from_result(\n"
        "                        item.to_block(), result, timestamp=self._clock()\n"
        "                    )\n"
        "                )",
    )


def _inject_e55(repo: Repo) -> None:
    """E56 / G56：让 loop 在**还有输入时**提前收工。

    把"模型不再要工具就完成"改成"跑完第一轮就完成"——
    此时 transcript 还剩好几轮没消费，而状态仍然是 ``completed``。
    **这正是"绿得看不出来"的形态**：不报错、状态正常，
    只是模型后面的输出全丢了。
    """
    repo.patch(
        LOOP,
        "            if not calls:  # 第 8 步：模型不再要工具 → 完成",
        "            if True:  # 注入：跑完第一轮就收工\n"
        "                finished = TurnResult(\n"
        '                    status="completed",\n'
        "                    messages=produced,\n"
        "                    text=last_text,\n"
        "                    rounds=round_index,\n"
        "                    usage=total_usage,\n"
        "                )\n"
        "                self._notify(_turn_end(finished))\n"
        "                return finished\n"
        "            if not calls:  # 第 8 步：模型不再要工具 → 完成",
    )


def _inject_e57(repo: Repo) -> None:
    """E57 / G57（**已废弃，保留代码留档**）：让所有场景共用同一个工作区。

    结果：**注入后仍然全绿**——这条门槛**证伪不了**，
    因此它**不是**一条门槛，只是一句"设计意图"。

    为什么證伪不了（2026-09-21 实测）
        1. pytest 给每个参数化用例**各自**的 ``tmp_path``，
           把 ``tmp_path`` 换成 ``tmp_path.parent`` 之后，
           不同用例之间**依然**是不同目录——"共用"没真正发生。
        2. 即便真的共用，``write_fixture_workspace`` 是**覆盖写**，
           而回放场景断言的是 **loop 行为**（轮数 / 工具调用数），
           不读工作区里的文件内容。残留一个 ``demo/out`` 目录
           对断言**毫无影响**。

    结论：``tests/fixtures/workspace.py`` 的隔离是**好实践**（它防的是
    "将来某个场景开始读文件内容"这类尚未发生的事），
    但它**现在不构成门槛**——没有任何注入能证伪它。

    **保留这段代码而不是删掉**：它记录了"我们试过、为什么不行"。
    删掉的话，下一个人会重新想一遍同样的问题。
    这段不在 ``main()`` 里注册，所以不会出现在实验结果里。
    """
    repo.patch(
        WORKSPACE_FIXTURE,
        "    for rel, content in _FILES.items():\n"
        "        target = root / rel\n"
        "        target.parent.mkdir(parents=True, exist_ok=True)\n"
        "        target.write_text(content, encoding=\"utf-8\")",
        "    # 注入：所有场景共用同一份目录（不隔离）\n"
        "    root.mkdir(parents=True, exist_ok=True)\n"
        "    for rel, content in _FILES.items():\n"
        "        target = root / rel\n"
        "        target.parent.mkdir(parents=True, exist_ok=True)\n"
        "        if not target.exists():\n"
        "            target.write_text(content, encoding=\"utf-8\")",
    )
    repo.patch(
        "tests/test_transcript_scenarios.py",
        "    assert isinstance(scenario, Scenario)\n"
        "    report = await run_scenario(scenario, tmp_path)",
        "    assert isinstance(scenario, Scenario)\n"
        "    report = await run_scenario(scenario, tmp_path.parent)  # 注入：共用父目录",
    )


def main() -> int:
    experiment(
        "G55",
        "单条工具失败不得中止整批（回放场景证据）",
        SELF_CORRECTION_TEST,
        _inject_e54,
    )
    experiment(
        "G56",
        "transcript 必须被完整消费（loop 不得提前收工）",
        SELF_CORRECTION_TEST,
        _inject_e55,
    )
    experiment(
        "G56b",
        "所有回放场景都不得提前收工",
        SCENARIO_TESTS,
        _inject_e55,
    )
    experiment(
        "G58",
        "TurnResult.usage 必须累计（不是最后一轮）",
        f"{LOOP_TESTS}::test_usage_is_summed_over_every_round",
        _inject_e58,
    )
    # G57（工作区隔离）**刻意不注册**——E57 实测无法证伪它。
    # 见 _inject_e57 的 docstring：宁可少一条门槛，不要一条名义门槛。

    print()
    print("=" * 78)
    print("P2-1 回放场景门槛注入实验结果")
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
    print("注：G57（回放工作区隔离）**未注册**——E57 注入后仍然全绿，")
    print("    它证伪不了，故不构成门槛。理由见 _inject_e57 的 docstring。")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
