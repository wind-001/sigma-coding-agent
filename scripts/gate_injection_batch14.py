"""P4-批次1 门槛注入实验：逐条证伪技能系统的门槛（G75–G79）。

沿用批次 2–4 / 7 / 8 / P2-x / P3-x 的框架（在真实仓库上改、跑、finally 还原），
**不另写一份 Repo**——验证工具自身会腐化，两份实现意味着修一个忘另一个。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E63 | G75 索引顺序稳定 | `discover_skills` 按名字**倒序**排 | 期望 `["alpha","zeta"]` 的用例红 |
| E64 | G76 正文不进常驻区 | `render_index` 把正文**也拼进去** | "加载正文后常驻区没变"红 |
| E65 | G77 认不出名字要列可用项 | 错误文案里删掉可用列表 | "结果里含 alpha/beta"红 |
| E66 | G78 坏技能必须被报告 | `discover_skills` 静默跳过坏目录 | "问题清单非空"红 |
| E67 | G79 截断说明不能被预算吃掉 | `load_body` 去掉**短标记退路** | 低预算档"必须返回非空文本"红 |

**E67 这条是补写的，因为它对应的缺陷是真实发生过的**

    详细标记里带**绝对路径**，路径长度随环境变（临时目录在 `AppData\\Local\\Temp`
    下约 90 字符，指进项目里 130+）。于是同一个 `max_tokens=200`：
    一种环境切得下，另一种环境下**标记自己就把预算吃光** → 返回空文本，
    **调用的模型既拿不到正文、也看不到"被截断了"**。

    它由**全量跑**暴露（单文件跑是绿的），根因是 `resources.py` 修过的同一个坑
    ——技能这边漏了短标记退路。倒过来说：这个坑之所以能活到全量跑，
    就是因为当时没有一条**与环境无关**的用例钉它。

**E64 这条注入不是编的——它就是架构 7.6 节那个 ablation**

    架构第 1170 行有一行对照实验：「技能全量注入 vs 按需 | 按需版本 | 常驻 token、cache 命中率」。
    E64 注入的效果**正好就是"全量注入"那一档**：把技能正文也塞进索引。
    所以这条注入同时也是那个 ablation 的"可实现版本"——
    它证明"按需"与"全量"在**常驻区字节数**上是可区分的，而不是理念之争。

**为什么 G75 的注入选"倒序"而不是"删掉排序"**

    "删掉排序"看似更贴近真实疏漏，但它**可能构造不出红**：
    本机文件系统恰好按名字返回条目时，删掉排序前后结果一样——
    实验会报"注入后仍然全绿"，而那是**实验无效**，不是门槛无效
    （与 P2-3 的 G51 预判不成立是同一类问题）。
    倒序是**确定性**的：只要排序还在，结果就一定不是倒序；反过来一定红。

    **同一条判据在 E67 上又用了一次**：档位下限取 25 而不是 15，
    因为 25 那一档的两个不等式（`44 < 3×25 < 145`）都留了余量，
    在任何机器上都成立；15 只差 1 个字符，是在碰运气。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch14.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

SKILLS = "core/sigma_agent/skills.py"
TOOL = "core/sigma_tools/skill.py"

TESTS = "tests/test_agent_skills.py"

ORDER_TEST = f"{TESTS}::test_discover_finds_skills_and_sorts_by_name"
ISOLATION_TEST = f"{TESTS}::test_body_never_enters_the_resident_region"
UNKNOWN_TEST = f"{TESTS}::test_tool_unknown_name_lists_available"
PROBLEM_TEST = f"{TESTS}::test_discover_reports_directory_without_markdown"
BUDGET_TEST = f"{TESTS}::test_discard_stays_visible_at_every_body_budget"


def _inject_e63(repo: Repo) -> None:
    """E63 / G75：索引顺序不稳定。

    后果是常驻区**每轮都变**——prompt cache 从变化点起全部失效，
    而失效是静默的（只是变慢变贵）。
    """
    repo.patch(
        SKILLS,
        "    skills.sort(key=lambda s: s.name)",
        "    # 注入：顺序不稳定\n    skills.sort(key=lambda s: s.name, reverse=True)",
    )


def _inject_e64(repo: Repo) -> None:
    """E64 / G76：把技能正文也拼进索引。

    **这就是架构 7.6 节那个「全量注入」档位。** 后果：
    每加一个技能，常驻区就长一截；而正文本来该是"用到了才付"的成本。
    """
    repo.patch(
        SKILLS,
        "    lines.extend(\n"
        '        f"- {skill.name}：{skill.description} → {skill.location}" for skill in skills\n'
        "    )",
        "    lines.extend(\n"
        '        f"- {skill.name}：{skill.description} → {skill.location}\\n'
        '  正文：{load_body(skill).text}"\n'
        "        for skill in skills\n"
        "    )",
    )


def _inject_e65(repo: Repo) -> None:
    """E65 / G77：认不出名字时不列可用项。

    模型只能瞎猜着重试，**而每次重试都是一次完整的模型调用**。
    """
    repo.patch(
        TOOL,
        '            available = "、".join(self.skill_names) or "（当前没有任何技能）"',
        '            available = "（当前没有任何技能）"  # 注入：不列可用项',
    )


def _inject_e66(repo: Repo) -> None:
    """E66 / G78：坏技能被静默跳过。

    症状是"我加了技能它怎么不用"——排查方向完全错，
    而且与"手工清单漂移"是同一种失败（都是静默丢失）。
    """
    repo.patch(
        SKILLS,
        '            problems.append(f"{directory.name}/：目录里没有 .md 文件，已跳过")',
        "            continue  # 注入：静默跳过空目录",
    )


def _inject_e67(repo: Repo) -> None:
    """E67 / G79：去掉**短标记退路**。

    详细标记里带**绝对路径**，而路径长度随环境变。去掉退路之后，
    预算一旦紧到装不下它，``load_body`` 就返回**空文本**——
    调用方既拿不到正文、也看不到「被截断了」。

    **这条对应的缺陷是真实发生过的**（2026-09-22）：它由全量跑暴露，
    单文件跑是绿的。根因是 ``resources.py`` 修过的同一个坑，
    技能这边漏了短标记退路——**同一个坑第二次踩到**。

    **为什么这条注入确定会红**（算式见文件头）：
    25 token 档下 ``44 < 3×25 < 145`` 两个不等式都留了余量，
    所以"详细装不下、短装得下"在任何机器上都成立。
    """
    repo.patch(
        SKILLS,
        "    return truncate_to_tokens(body, max_tokens, markers=(marker, short_marker))",
        "    # 注入：去掉短标记退路\n"
        "    return truncate_to_tokens(body, max_tokens, markers=(marker,))",
    )


def main() -> int:
    experiment("G75", "索引顺序稳定（两次渲染逐字节相同）", ORDER_TEST, _inject_e63)
    experiment("G76", "正文不进常驻区（=全量注入档位）", ISOLATION_TEST, _inject_e64)
    experiment("G77", "认不出技能名时列出可用项", UNKNOWN_TEST, _inject_e65)
    experiment("G78", "坏技能目录必须被报告", PROBLEM_TEST, _inject_e66)
    experiment("G79", "截断说明不能被预算吃掉（短标记退路）", BUDGET_TEST, _inject_e67)

    print()
    print("=" * 78)
    print("P4-批次1 技能系统门槛注入实验结果")
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
    print("注：E64 注入的效果就是架构 7.6 节那个「技能全量注入 vs 按需」的对照档位。")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
