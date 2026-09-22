"""P2-4 门槛注入实验：逐条证伪压缩门槛（G51 / G52 / G60）。

沿用批次 2–4 / 7 / 8 / P2-1…P2-3 的框架（在真实仓库上改、跑、finally 还原），
**不另写一份 Repo**——验证工具自身会腐化，两份实现意味着修一个忘另一个。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E65 | G51 摘要降级成 `user` | `to_llm()` 改成返回 `SystemMessage` | 类型断言红 |
| E66 | G51b 摘要不进常驻区 | 把摘要拼进 `_resident_text()` | 压缩后指纹变了 |
| E67 | G52 三条保真要求 | 从提示词里删掉「失败过什么」 | 对应用例红 |
| E68 | G60 压缩不改历史 | `history()` 改返回有效视图 | 原始历史条数变了 |

**G51 为什么要点两条注入**

    P2 详规 §5 原本预判的注入是「改成 `system` → 常驻区哈希变化（G50 连带红）」。
    **实测这个预判不成立**：常驻区指纹是从 `_resident_text()`（系统提示词 +
    项目说明）与工具 schema 算出来的，**与历史无关**——
    所以把摘要的 `to_llm()` 改成 `SystemMessage`，指纹**一点都不会变**。

    这条门槛其实是两件独立的事：
    ① 摘要**降级成什么**（类型层，错在"模型会把它当成系统指令"）；
    ② 摘要**在不在常驻区**（位置层，错在"每压一次 prompt cache 全失效"）。
    所以拆成 E65 / E66 两条注入，各证伪一半。
    **预判错在这里是好事**：说明这条门槛原本只被想成了一条。

**E68 为什么值得单独一条**

    "压缩不改历史"是本项目对 P2 详规 Q3 的**有意偏离**（Q3 说"替换"）。
    偏离的理由写在两份 docstring 里，但**注释挡不住重构**——
    后来者完全可能觉得"history() 应该返回模型看到的东西"而顺手合并两者。
    那一步不会报任何错，只会让**审计链静默消失**。
    所以要用一条能变红的断言把它钉住。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch11.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

COMPACT = "core/sigma_session/compact.py"
CONTEXT = "core/sigma_session/context.py"

COMPACT_TESTS = "tests/test_session_compact.py"

USER_MESSAGE_TEST = f"{COMPACT_TESTS}::test_summary_degrades_to_user_not_system"
FINGERPRINT_TEST = (
    f"{COMPACT_TESTS}::test_compaction_does_not_change_the_resident_region"
)
PROMPT_TEST = f"{COMPACT_TESTS}::test_summary_prompt_requires_each_fidelity_item"
RAW_HISTORY_TEST = f"{COMPACT_TESTS}::test_raw_history_is_untouched_by_compaction"


def _inject_e65(repo: Repo) -> None:
    """E65 / G51：摘要不降级成 ``user``，而是降级成 ``system``。

    后果不是"类型不对"，而是**模型会把一段历史摘要当成系统指令**——
    摘要里写着"用户要求删掉 X"，模型就会去删 X，而那是已经发生过的事。
    """
    repo.patch(
        COMPACT,
        "        return UserMessage(content=self.render(), timestamp=self.timestamp)",
        "        return SystemMessage(content=self.render(), timestamp=self.timestamp)"
        "  # 注入：降级成 system",
    )


def _inject_e66(repo: Repo) -> None:
    """E66 / G51b：把摘要拼进常驻区。

    这是"每压一次 prompt cache 全失效"的形态：压缩本来是为了省 token，
    结果让整段常驻前缀每次都重算——**越压越贵**，而且完全静默。
    """
    repo.patch(
        CONTEXT,
        '        if not self._project_instructions:\n'
        '            return self._system_prompt\n'
        '        return f"{self._system_prompt}\\n\\n{self._project_instructions}"',
        "        base = (\n"
        "            self._system_prompt\n"
        "            if not self._project_instructions\n"
        '            else f"{self._system_prompt}\\n\\n{self._project_instructions}"\n'
        "        )\n"
        "        if self._summary is not None:  # 注入：把摘要塞进常驻区\n"
        '            base = f"{base}\\n\\n{self._summary.summary}"\n'
        "        return base",
    )


def _inject_e67(repo: Repo) -> None:
    """E67 / G52：从摘要提示词里删掉「失败过什么」。

    后果是 agent **重复踩已经踩过的坑**，而症状看起来像"模型不够聪明"——
    离根因（提示词少了一条要求）非常远。
    """
    repo.patch(
        COMPACT,
        "3. 失败过什么 —— 试过但没成功的方向，以及失败的原因。\n"
        "   这一条最容易漏，但**漏了 agent 会重复踩同一个坑**。\n",
        "",
    )


def _inject_e68(repo: Repo) -> None:
    """E68 / G60：把 ``history()`` 改成返回**有效视图**。

    这是最可能发生的一次"顺手重构"：有人看到
    ``history()`` 与 ``effective_history()`` 两个方法，觉得后者才是
    "真正在用的那个"，于是把前者也改成它。**不会报任何错**，
    只是原始历史（审计凭据）从此不可得。
    """
    repo.patch(
        CONTEXT,
        "        return self._tree.history()\n\n    # ------------------------------------------------------------------\n"
        "    # 压缩（视图，不改历史）",
        "        return self.effective_history()  # 注入：把视图当成历史\n\n"
        "    # ------------------------------------------------------------------\n"
        "    # 压缩（视图，不改历史）",
    )


def main() -> int:
    experiment("G51", "摘要降级成 user（不是 system）", USER_MESSAGE_TEST, _inject_e65)
    experiment("G51b", "摘要不进常驻区（压缩后指纹不变）", FINGERPRINT_TEST, _inject_e66)
    experiment("G52", "摘要提示词含三条保真要求", PROMPT_TEST, _inject_e67)
    experiment("G60", "压缩不改历史（history 是原始历史）", RAW_HISTORY_TEST, _inject_e68)

    print()
    print("=" * 78)
    print("P2-4 压缩门槛注入实验结果")
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
    print("注：G51 拆成两条注入。P2 详规原预判「改成 system → 常驻区哈希变化」")
    print("    **不成立**——指纹与历史无关。类型层与位置层是两件独立的事。")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
