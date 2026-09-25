"""P2 第 3 条验收门的**离线代理门槛**注入实验（G73 / G74）。

为什么需要这个脚本
    P2 的硬性验收门第 3 条是「压缩前后成功率下降 ≤ 5%」。
    它**离线测不了**：那需要真模型理解任务（见 P2 详规 §14.5）。
    所以在 70 条任务集落地之前，这条门只有两个状态：
    「未验收」和「未验收」，没有第三种。

    但**"测不了总量"不等于"什么都不必钉"**。可以拆出两个
    **离线可测、可被注入证伪**的子事实——它们是真指标的必要条件，
    但不是充分条件：

    | 编号 | 子事实 | 为什么它是必要条件 |
    | --- | --- | --- |
    | G73 | 压缩视图的**摘要消息真的发给了模型** | 摘要若被静默丢弃，压缩退化成"直接扔掉历史"。成功率会掉，而**掉在一个与压缩质量无关的地方** |
    | G74 | 保留窗口 = **最近 N 轮**，距 head 最近的那一轮绝不被切掉 | 切分点算错会丢掉"刚刚发生的事"。模型于是重做已完成的工作——症状看起来只是"这次答得不好" |

    **口径必须写死在报告里**：G73/G74 全绿**不能**推出第 3 条验收门通过。
    它们只排除两类"离线就能排除的坏法"。真结论仍然等任务集。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_p2_compaction.py

    （前台跑；一次最多两个脚本；若大量 FAIL 且原因含 BULK_CONFIRM_REQUIRED，
     那是本机沙箱噪声，重跑即可。）
"""

from __future__ import annotations

import re
import sys

from gate_injection_batch24 import REPO, RESULTS, Repo, experiment

CONTEXT = "core/sigma_session/context.py"

COMPACT_TESTS = "tests/test_session_compact.py"

#: 断言名模糊匹配的关键词（避免把用例名/行号写死——那是会腐化的锚点）。
#:
#: G73 用 ``resident_region``——`test_compaction_does_not_change_the_resident_region`
#: 是**唯一**穿过 ``build_messages()`` 的压缩用例。它名义上验"指纹不变"，
#: 但末尾有一段"摘要确实在系统消息**之后**"的断言：若摘要没进组装，
#: 那个 ``any(...)`` 会假手于**原文里恰好含"摘要"二字**而……实测会红
#: （见注入 E73 的实测结果）。所以它同时是"摘要真的发出去了"的锚点。
#: ⚠️ 这个锚点比理想情况弱——理想锚点应当直接断言"模型看到的最后一条 user 是摘要"。
#: **待补**：新增一条专门的 G73 断言用例，再把锚点换过去。
SUMMARY_HINTS = ("resident_region",)
KEEP_HINTS = ("kept_window", "split_keeps")


def _inject_e73(repo: Repo) -> None:
    """E73 / G73：摘要不进 ``build_messages``。

    ``effective_history()`` 仍然返回 ``[摘要, *保留段]``（视图层没错），
    但组装给 loop 的列表只取 ``[system, *full histories]``——
    等价于"压缩被静默降级成丢弃历史"。

    这是**最像"压缩效果不好"的一种坏法**：没有异常、没有 warning，
    只是模型看到的上下文里少了一段。症状被归因到"模型不够聪明"，
    而实际是 harness 把准备好的东西扔了。
    """
    repo.patch(
        CONTEXT,
        "        return [system, *self.effective_history()]",
        "        # 注入：摘要不进组装（压缩退化为丢弃历史）\n"
        "        return [system, *self._tree.history()]",
    )


def _inject_e74(repo: Repo) -> None:
    """E74 / G74：保留窗口切错，丢掉离 head 最近的一轮。

    把 ``skip`` 从"要跳过的条数"改成"少跳一条"——于是保留窗口整体
    往前挪一格，**最近的一轮被当成历史丢掉了**。

    真做错时更可能的形态是"切分点按原始轮次算、而不是按有效视图算"，
    在本机表现为同一类后果：**刚刚发生的事不在上下文里**。
    """
    repo.patch(
        CONTEXT,
        "        skip = len(path) - 1\n        return [self._summary, *full[skip:]]",
        "        # 注入：切分点往前挪一格（丢掉最近一轮）\n"
        "        skip = max(0, len(path) - 2)\n"
        "        return [self._summary, *full[skip:]]",
    )


def _resolve_tests(hints: tuple[str, ...]) -> list[str]:
    """在 `COMPACT_TESTS` 里找出名字含**任一** ``hint`` 的用例。

    不写死用例名：写死会让"哪天用例改名了"变成一个**看起来像门槛失效**的假红。
    也**不硬编码行号或参数化 id**——那是同一类腐化。
    """
    src = (REPO / COMPACT_TESTS).read_text(encoding="utf-8")
    found: list[str] = []
    for hint in hints:
        pattern = r"^\s*(?:async\s+)?def (test_[A-Za-z0-9_]*" + re.escape(hint) + r"[A-Za-z0-9_]*)"
        for name in re.findall(pattern, src, re.M):
            ref = f"{COMPACT_TESTS}::{name}"
            if ref not in found:
                found.append(ref)
    return found


def main() -> int:
    summary_tests = _resolve_tests(SUMMARY_HINTS)
    keep_tests = _resolve_tests(KEEP_HINTS)

    if not summary_tests:
        print(f"[FATAL] 在 {COMPACT_TESTS} 里找不到含 {SUMMARY_HINTS} 的用例——"
              "门槛锚点已失效，先修这个脚本再跑实验。", file=sys.stderr)
        return 2
    if not keep_tests:
        print(f"[FATAL] 在 {COMPACT_TESTS} 里找不到含 {KEEP_HINTS} 的用例——"
              "门槛锚点已失效，先修这个脚本再跑实验。", file=sys.stderr)
        return 2

    print(f"[信息] G73 锚点 {len(summary_tests)} 条：{summary_tests}")
    print(f"[信息] G74 锚点 {len(keep_tests)} 条：{keep_tests}")

    experiment("G73", "摘要真的发给了模型（压缩不是静默丢弃）", summary_tests, _inject_e73)
    experiment("G74", "保留窗口含最近 N 轮（不丢刚发生的事）", keep_tests, _inject_e74)

    print()
    print("=" * 78)
    print("P2 第 3 条验收门 · 离线代理门槛实验")
    print("=" * 78)
    ok = 0
    for gate, what, passed, detail in RESULTS:
        flag = "PASS" if passed else "FAIL"
        ok += int(passed)
        print(f"[{flag}] {gate}  {what}")
        if not passed:
            print(f"        {detail}")
    print("-" * 78)
    print(f"{ok}/{len(RESULTS)} 通过")
    print()
    print("⚠️ 口径：本实验全绿 **不等于** 「压缩前后成功率下降 ≤ 5%」通过。")
    print("   它只排除两类离线可排除的坏法。第 3 条验收门在 evals/datasets/")
    print("   的 70 条任务集落地并跑出对照之前，状态始终是**未验收**。")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
