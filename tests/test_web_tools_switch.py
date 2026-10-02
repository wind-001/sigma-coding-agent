"""联网工具开关(``InteractiveSession.set_web_tools``)的门槛测试。

为什么单独一个文件：这条路径换的是**三样**东西（registry / context / loop），
而每一处都可以"看起来对"而实际漏参——漏 ``tool_lock`` 子agent 直接瘫、
漏 ``ask`` ask_user 退化成自动选项、漏 ``hooks`` 消息只进内存不落盘。
所以门要覆盖的是**换表之后整份组装还成立**，不是"工具表变了"这一件事。

配套：``test_workbench_server.py`` 里的 G91/G92/G93 覆盖产品壳那一侧
（装配路径 + 提示词同源 + 常驻区预算）。本文件覆盖 SDK 方法本身，
两边合起来才闭环——只测一边，另一边的少传参数仍然测不出来。

对照的既有纪律：``test_hot_reload.py`` 测的是同一种"会话内换表"，
复用它的判据（换表后指纹重算不炸、历史保留、persist 钩子接到新 context）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sigma.sdk import InteractiveSession


class _QuietProvider:
    """不发网络请求的假 provider（本文件不跑轮，只测装配）。"""

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def estimate_tokens(self, messages: list[Any]) -> int:
        return 0


def _session(tmp_path: Path, **kwargs: Any) -> InteractiveSession:
    (tmp_path / "AGENTS.md").write_text("test", encoding="utf-8")
    return InteractiveSession(
        provider=_QuietProvider(),  # type: ignore[arg-type]
        workspace_root=tmp_path,
        model="test-model",
        enable_trace=False,
        enable_checkpoint=False,
        **kwargs,
    )


def test_web_tools_toggle_roundtrip(tmp_path: Path) -> None:
    """开关往返：开→工具与提示词都出现；关→两者都消失。"""
    session = _session(tmp_path)
    assert "web_search" not in session._registry.names()

    session.set_web_tools(web_search=True, tavily_api_key="tvly-test")
    assert "web_search" in session._registry.names()
    assert "web_search" in session._context._system_prompt

    session.set_web_tools()
    assert "web_search" not in session._registry.names()
    assert "web_search" not in session._context._system_prompt


def test_prompt_and_registry_never_disagree(tmp_path: Path) -> None:
    """**同源判据**：工具表与提示词要么都有 web_search，要么都没有。

    这是本批次的核心纪律。只查一侧的测试会漏掉真正的故障：
    - 只查注册表 → 漏掉"工具在、提示词不提"（模型不知道能调）；
    - 只查提示词 → 漏掉"提示词说能调、表里没有"（模型调一个不存在的工具）。

    两种症状看起来一样：联网功能"像是坏了"。
    """
    session = _session(tmp_path)
    for kwargs in (
        {},
        {"web_search": True, "tavily_api_key": "tvly-test"},
        {"web_fetch": True, "firecrawl_api_key": "fc-test"},
        {"web_search": True, "tavily_api_key": "tvly-test", "web_fetch": True,
         "firecrawl_api_key": "fc-test"},
    ):
        session.set_web_tools(**kwargs)  # type: ignore[arg-type]
        in_table = "web_search" in session._registry.names()
        in_prompt = "web_search" in session._context._system_prompt
        assert in_table == in_prompt, f"{kwargs}: 表={in_table} 提示词={in_prompt}"


def test_resident_region_survives_toggle(tmp_path: Path) -> None:
    """换表后指纹与预算都要重新校验通过（名义门槛比没有更坏——要真跑）。

    换 schema 必然改指纹，不重新冻结的话下一轮 ``verify_resident_region``
    会当场抛 ``ResidentRegionChanged``。
    """
    session = _session(tmp_path)
    for kwargs in (
        {"web_search": True, "tavily_api_key": "tvly-test"},
        {},
        {"web_search": True, "tavily_api_key": "tvly-test"},
    ):
        session.set_web_tools(**kwargs)  # type: ignore[arg-type]
        session._context.verify_resident_region()
        session._context.verify_resident_budget()


def test_toggle_is_byte_stable(tmp_path: Path) -> None:
    """往返一次后常驻区**逐字节回到原状**。

    实测踩过的坑：重建提示词时按 ``build_system_prompt`` 的默认开关拼，
    而构造期用的是裸``SYSTEM_PROMPT``（两者 ``memory`` 语义不同）——
    往返一次常驻区多出 62 token，看着像漂移，实际是重建时用了不同的开关。
    这条门把"其他开关一个都不能动"钉死。
    """
    session = _session(tmp_path)
    before = session._context._system_prompt
    before_tokens = session._context.resident_tokens

    session.set_web_tools(web_search=True, tavily_api_key="tvly-test")
    session.set_web_tools()

    assert session._context._system_prompt == before
    assert session._context.resident_tokens == before_tokens


def test_loop_follows_the_new_registry(tmp_path: Path) -> None:
    """**换表必须换 loop**：``AgentLoop`` 在构造期持有 registry 引用。

    这条最容易被当成多余——"换个表而已"。实测反证：不换 loop 的话
    loop 照样调旧表，而模型看到的常驻区里是新表：它调web_search，
    loop 拿旧表去查，报"未注册的工具"，而用户看到的是"工具明明列出来了"。
    """
    session = _session(tmp_path)
    assert session._loop._registry is session._registry

    session.set_web_tools(web_search=True, tavily_api_key="tvly-test")
    assert session._loop._registry is session._registry, (
        "loop 仍持有旧 registry —— 换表不换 loop，模型调新工具会被判未注册"
    )
    assert "web_search" in session._loop._registry.names()


def test_missing_key_raises_instead_of_silently_disabling(tmp_path: Path) -> None:
    """要开但没key：明确抛，不静默降级。

    静默降级是本缺陷的同款病：装配层少做一件事，界面上看不出来，
    只有 agent 说的话暴露它。这里抛出来，让调用方能告诉用户"先配 key"。
    """
    session = _session(tmp_path)
    with pytest.raises(ValueError, match="tavily_api_key"):
        session.set_web_tools(web_search=True)
    # 关的那一侧不需要 key（默认态就是关，不该强制传一个用不上的参数）
    session.set_web_tools()  # 不抛即通过


def test_hooks_survive_toggle(tmp_path: Path) -> None:
    """换表后持久化钩子必须**接到新 context**（否则消息只进内存不落盘）。

    这是 ``reload_tools`` 早就踩过的坑：重建 context 之后忘了 rebind,
    表现是"任务跑完了但会话文件是空的"。换表走同一条路，所以同一条门。
    """
    session = _session(tmp_path)
    session.set_web_tools(web_search=True, tavily_api_key="tvly-test")
    #用公开的 .context 而不是 _context：那个公开属性存在的理由
    # 就是"rebind 的接线必须可被从外部断言"（见 hooks/persist.py），
    # 私有属性一改名测试就失效。
    assert session._persist_hook.context is session._context, (
        "持久化钩子仍指着旧 context —— 换表后的消息不会被持久化"
    )