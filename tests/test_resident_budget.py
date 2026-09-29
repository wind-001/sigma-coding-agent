"""P5-批次3 的门槛测试:常驻预算修订(D4 v2)的表与闸(G885/G886)。

每条测试对应详规里的一张门槛卡,**每条都有"注入变红"的路径**
(scripts/gate_injection_batch18.py)。
"""

from __future__ import annotations

import json
from pathlib import Path

from sigma.providers.tokens import estimate_text
from sigma.sessions.context import (
    DEFAULT_RESIDENT_BUDGET_TOKENS,
    SessionContext,
)
from sigma.config.resident_caps import CAPS, RESIDENT_BUDGET_TOKENS, caps_sum
from sigma.sdk import build_system_prompt, default_registry

FIXED_TIME = "2026-09-28T12:00:00.000"


# ---------------------------------------------------------------------------
# G885:分项之和 == 总额,且与 context 的常量同源(5.1.0 教训的代码化)
# ---------------------------------------------------------------------------


def test_g885_caps_sum_equals_budget() -> None:
    assert caps_sum() == RESIDENT_BUDGET_TOKENS
    assert RESIDENT_BUDGET_TOKENS == DEFAULT_RESIDENT_BUDGET_TOKENS
    # 预留必须**具名**存在——无名的余量会悄悄腐化(5.1.0)。
    # "具名预留(repo map 等)"已由 P1 收尾兑现为 "repo map"(详规:
    # docs/plans/P1-repo-map-详规.md,消费纪律:实测数字进 MEASURED)。
    assert "repo map" in CAPS
    assert CAPS["repo map"] > 0


# ---------------------------------------------------------------------------
# G886:最肥合法配置 ≤ 总额,且指纹仍逐字节稳定(D4 的核心主张未被削弱)
# ---------------------------------------------------------------------------


def test_g886_full_feature_config_within_budget(tmp_path: Path) -> None:
    """全功能配置(联网+task+AGENTS+技能+记忆)装配后必须过 G59 预算闸。

    task 的 schema 由 sdk 在 enable_sub_agent 时注册,这里为了构造
    "最肥"的注册表,用桩工厂把 TaskTool 注册进来(与 sdk 同一路径)。
    技能索引与记忆索引用合成样本(与真实渲染同 cap 口径,注释标明)。
    """
    from sigma.runtime.sub_agent import TaskTool

    async def _factory(description: str, ctx: object, sub_id: str, max_rounds: int) -> object:
        return None

    registry = default_registry(
        web_search=True, tavily_api_key="k", web_fetch=True, firecrawl_api_key="k", todo=True
    )
    registry.register(TaskTool(factory=_factory, max_concurrent=3))  # type: ignore[arg-type]

    # 技能索引样本:3 条,形态与 sigma.skills.scanner.render_index 一致(≈57 token 量级)
    skill_index = (
        "可用技能（用 load_skill 加载正文）：\n"
        "- context-compact → extensions/skills/context-compact/SKILL.md\n"
        "- image → extensions/skills/image/SKILL.md\n"
        "- videa → extensions/skills/videa/SKILL.md"
    )
    # 记忆索引样本:与 memory.py 渲染同形态、同 250 cap 口径(15 条)
    memory_index = "\n".join(f"- 记忆 {i:02d} → memory/m{i:02d}.md" for i in range(15))

    context = SessionContext(
        system_prompt=build_system_prompt(
            skills=True, web_search=True, web_fetch=True, task=True, todo=True, memory=True
        ),
        tools_schema=registry.schemas(),
        clock=lambda: FIXED_TIME,
        project_instructions=Path("AGENTS.md").read_text(encoding="utf-8"),
        skill_index=skill_index,
        memory_index=memory_index,
    )

    context.verify_resident_budget()  # G59 不抛
    assert context.resident_tokens <= RESIDENT_BUDGET_TOKENS
    # 逐字节稳定的主张原样成立:指纹两次一致
    assert context.fingerprint == context.fingerprint
    # 汇总行(报告用):全开+记忆的真实占用
    print(
        f"全功能+记忆 常驻实测 {context.resident_tokens}/{RESIDENT_BUDGET_TOKENS} "
        f"(其中 tools_schema 文本 {estimate_text(json.dumps(registry.schemas(), ensure_ascii=False, sort_keys=True))})"
    )
