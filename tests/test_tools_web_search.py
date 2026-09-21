"""web_search 工具的测试：记账口径 / 额度硬闸 / 错误不穿透。

全部离线：httpx.MockTransport 注入假响应，不需要 API key、不联网——
"无 key 全绿"是既有纪律，新工具不能破例（批次 7 详规第 7 节）。

三处容易写错、且有专门门槛钉住的地方
    1. 额度判定必须在**发请求之前**（G37）——否则退化成"先花钱再发现没钱"；
    2. 口径是 credits 不是次数（G38）——advanced 一次 2 credits；
    3. 并发下"校准 + 判定 + 预留"必须是原子的（G39）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from sigma_agent.types import ToolContext
from sigma_ai.base import NeverCancelled
from sigma_tools._tavily_quota import FREE_MONTHLY_CREDITS, TavilyQuota
from sigma_tools.web_search import MAX_SNIPPET_CHARS, WebSearchTool

NOW = 1000.0


def _ctx(root: Path) -> ToolContext:
    return ToolContext(session_id="test", workspace_root=root, signal=NeverCancelled())


def _payload(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "query": "tavily free tier",
        "response_time": 0.42,
        "results": [
            {
                "title": "Credits & Pricing",
                "url": "https://docs.tavily.com/api-credits",
                "content": "You get 1,000 free API Credits every month.",
                "score": 0.93,
            },
            {
                "title": "Search API",
                "url": "https://docs.tavily.com/search",
                "content": "POST /search executes a query.",
                "score": 0.71,
            },
        ],
    }
    data.update(over)
    return data


def _seed_state(
    tmp_path: Path,
    *,
    used: int = 0,
    limit: int = FREE_MONTHLY_CREDITS,
    synced_at: float = NOW,
    disabled: bool = False,
) -> Path:
    """直接落一份状态文件。

    synced_at=NOW 且 clock 固定为 NOW 时快照算"新鲜"，
    于是不会有任何 /usage 请求——这正是 G37"零网络请求"要的初始条件。
    """
    path = tmp_path / "tavily_usage.json"
    path.write_text(
        json.dumps(
            {
                "cycle_used": used,
                "cycle_limit": limit,
                "synced_at": synced_at,
                "disabled": disabled,
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )
    return path


def _handler(
    calls: list[str],
    *,
    usage: tuple[int, int] = (0, FREE_MONTHLY_CREDITS),
    payload: dict[str, Any] | None = None,
    search_status: int = 200,
    search_text: str | None = None,
    usage_status: int = 200,
) -> Any:
    """一个 MockTransport handler：按路径分派 /usage 与 /search。"""

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/usage":
            if usage_status >= 400:
                return httpx.Response(usage_status, json={"detail": "usage boom"})
            return httpx.Response(
                200,
                json={"key": {"usage": usage[0], "limit": usage[1]}},
            )
        if request.url.path == "/search":
            if search_text is not None:
                return httpx.Response(search_status, text=search_text)
            if search_status >= 400:
                return httpx.Response(search_status, json={"detail": "search boom"})
            return httpx.Response(200, json=payload if payload is not None else _payload())
        return httpx.Response(404, json={"detail": "unexpected path"})

    return handle


def _tool(
    tmp_path: Path,
    calls: list[str],
    *,
    state_path: Path | None = None,
    **handler_kwargs: Any,
) -> WebSearchTool:
    transport = httpx.MockTransport(_handler(calls, **handler_kwargs))
    quota = TavilyQuota(
        api_key="tvly-test",
        state_path=state_path or (tmp_path / "tavily_usage.json"),
        transport=transport,
        clock=lambda: NOW,
    )
    return WebSearchTool(api_key="tvly-test", quota=quota, transport=transport)


async def _run(tool: WebSearchTool, ctx: ToolContext, **kwargs: Any) -> Any:
    args = tool.params.model_validate({"query": "tavily free tier", **kwargs})
    return await tool.run(args, ctx)


# ----------------------------------------------------------------------
# 正常路径
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_titles_urls_and_charges_one_credit(tmp_path: Path) -> None:
    """正常路径：每条结果带标题 + URL；basic 档记 1 credit。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls)

    result = await _run(tool, _ctx(tmp_path))

    assert not result.is_error
    text = result.content[0].text
    assert "Credits & Pricing" in text
    assert "https://docs.tavily.com/api-credits" in text
    assert result.details["credits_used"] == 1
    assert result.details["credits_total"] == 1
    assert result.details["remaining"] == FREE_MONTHLY_CREDITS - 1
    assert result.details["results"][0]["url"] == "https://docs.tavily.com/api-credits"
    # 第一次调用必然过一次 /usage 校准（synced_at=0 视为过期）
    assert calls == ["/usage", "/search"]


@pytest.mark.asyncio
async def test_advanced_depth_costs_two_credits(tmp_path: Path) -> None:
    """G38：口径是 credits——advanced 一次 2 credits，不是 2 次。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls)

    result = await _run(tool, _ctx(tmp_path), search_depth="advanced")

    assert not result.is_error
    assert result.details["credits_used"] == 2
    assert result.details["credits_total"] == 2
    assert tool.quota.snapshot().cycle_used == 2


@pytest.mark.asyncio
async def test_long_snippet_is_capped_per_result(tmp_path: Path) -> None:
    """边界：单条摘要先截断，整段输出再截断。

    与 grep 的单行 240 字符同一条教训——第一条结果不该吃光整个输出预算。
    """
    calls: list[str] = []
    huge = "字" * (MAX_SNIPPET_CHARS * 3)
    tool = _tool(tmp_path, calls, payload=_payload(results=[{"title": "t", "url": "u", "content": huge}]))

    result = await _run(tool, _ctx(tmp_path))

    text = result.content[0].text
    assert "…" in text
    assert len(text) < MAX_SNIPPET_CHARS * 2
    assert result.details["truncated"] is False  # 8 KB 上限还没到


# ----------------------------------------------------------------------
# 额度硬闸（G37）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_quota_makes_no_search_request(tmp_path: Path) -> None:
    """G37：额度已用尽时**零 /search 请求**，且文案必须给行动指引。

    断言的是 /search 而不是"零网络请求"：/usage 校准是免费端点，
    而且它是"本地计数落后于现实"时的唯一发现手段——把它排除在外会让门槛变成
    "连校准都不许做"，那是错的。真正不能发生的是**花钱的那个请求**。
    """
    calls: list[str] = []
    state = _seed_state(tmp_path, used=FREE_MONTHLY_CREDITS, disabled=True)
    tool = _tool(tmp_path, calls, state_path=state, usage=(FREE_MONTHLY_CREDITS, FREE_MONTHLY_CREDITS))

    result = await _run(tool, _ctx(tmp_path))

    assert result.is_error
    assert calls.count("/search") == 0
    text = result.content[0].text
    assert "不要" in text and "web_search" in text
    assert result.details["quota_exhausted"] is True


@pytest.mark.asyncio
async def test_new_billing_cycle_reenables_the_tool(tmp_path: Path) -> None:
    """额度用尽后**不是永久禁用**：服务端说新周期用了 0，就自动恢复。

    这条性质很容易被写丢——把 disabled 做成"写死不再检查"，
    症状是"下个月也不能用了"，而用户只会以为 key 坏了。
    """
    calls: list[str] = []
    state = _seed_state(tmp_path, used=FREE_MONTHLY_CREDITS, disabled=True)
    tool = _tool(tmp_path, calls, state_path=state, usage=(0, FREE_MONTHLY_CREDITS))

    result = await _run(tool, _ctx(tmp_path))

    assert not result.is_error
    assert tool.quota.snapshot().disabled is False
    assert tool.quota.snapshot().cycle_used == 1


@pytest.mark.asyncio
async def test_server_usage_syncs_local_counter_and_blocks_next_call(tmp_path: Path) -> None:
    """校准：服务端说 999/1000，本地记 0 —— 以服务端为准，第 2 次调用被拦。"""
    calls: list[str] = []
    # 假服务端必须**跟着记账**：只回一个固定值的话，校准会把本地刚加的 1 打回去，
    # 那是假环境造出来的假故障（真实 /usage 会包含我们这一次）。
    server = {"used": 999}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/usage":
            return httpx.Response(
                200, json={"key": {"usage": server["used"], "limit": FREE_MONTHLY_CREDITS}}
            )
        server["used"] += 1
        return httpx.Response(200, json=_payload())

    transport = httpx.MockTransport(handler)
    quota = TavilyQuota(
        api_key="tvly-test",
        state_path=_seed_state(tmp_path, used=0, synced_at=0.0),  # 0 → 过期 → 强制校准
        transport=transport,
        clock=lambda: NOW,
    )
    tool = WebSearchTool(api_key="tvly-test", quota=quota, transport=transport)

    first = await _run(tool, _ctx(tmp_path))
    assert not first.is_error
    assert first.details["synced_with_server"] is True
    assert tool.quota.snapshot().cycle_used == FREE_MONTHLY_CREDITS  # 999 + 1

    calls.clear()
    second = await _run(tool, _ctx(tmp_path))
    assert second.is_error
    assert calls.count("/search") == 0  # 已禁用 → 不再花额度


@pytest.mark.asyncio
async def test_usage_endpoint_failure_falls_back_to_local_counter(tmp_path: Path) -> None:
    """校准失败不阻断：本地计数兜住，并且留下 last_error 供排查。"""
    calls: list[str] = []
    state = _seed_state(tmp_path, used=5, synced_at=0.0)
    tool = _tool(tmp_path, calls, state_path=state, usage_status=500)

    result = await _run(tool, _ctx(tmp_path))

    assert not result.is_error
    assert result.details["synced_with_server"] is False
    assert tool.quota.snapshot().cycle_used == 6  # 5 + 1
    assert tool.quota.snapshot().last_error is not None


@pytest.mark.asyncio
async def test_corrupt_state_file_resets_instead_of_crashing(tmp_path: Path) -> None:
    """边界：状态文件坏了 → 重置为 0 并记 last_error，**不崩**。

    "宁可多花一个 credit，不可让整个 agent 卡死在一个坏文件上"。
    """
    calls: list[str] = []
    state = tmp_path / "tavily_usage.json"
    state.write_text("{ 这不是 JSON", encoding="utf-8")
    tool = _tool(tmp_path, calls, state_path=state)

    result = await _run(tool, _ctx(tmp_path))

    assert not result.is_error
    assert tool.quota.snapshot().cycle_used == 1


# ----------------------------------------------------------------------
# 错误不穿透（G41）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_is_error_and_refunds_reservation(tmp_path: Path) -> None:
    """401：返回 is_error 而不抛异常；确定未执行 → 退回预留。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, search_status=401)

    result = await _run(tool, _ctx(tmp_path))

    assert result.is_error
    assert "TAVILY_API_KEY" in result.content[0].text
    assert tool.quota.snapshot().cycle_used == 0


@pytest.mark.asyncio
async def test_rate_limit_is_error_and_refunds_reservation(tmp_path: Path) -> None:
    calls: list[str] = []
    tool = _tool(tmp_path, calls, search_status=429)

    result = await _run(tool, _ctx(tmp_path))

    assert result.is_error
    assert "限流" in result.content[0].text
    assert tool.quota.snapshot().cycle_used == 0


@pytest.mark.asyncio
async def test_server_reported_quota_exhaustion_disables_tool(tmp_path: Path) -> None:
    """服务端说额度用尽（432）：本地账本对齐到上限并禁用。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, search_status=432)

    result = await _run(tool, _ctx(tmp_path))

    assert result.is_error
    snapshot = tool.quota.snapshot()
    assert snapshot.disabled is True
    assert snapshot.cycle_used == snapshot.cycle_limit


@pytest.mark.asyncio
async def test_non_json_response_is_error(tmp_path: Path) -> None:
    """响应不是 JSON（网关 HTML 等）：转成 is_error，不抛。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, search_text="<html>502 Bad Gateway</html>", search_status=200)

    result = await _run(tool, _ctx(tmp_path))

    assert result.is_error
    assert "不是 JSON" in result.content[0].text


@pytest.mark.asyncio
async def test_timeout_keeps_the_reservation(tmp_path: Path) -> None:
    """超时：**不退还**预留。

    请求可能已经在服务端发生。漏记是越界，多记只是保守——
    漂移由 /usage 校准收敛。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/usage":
            return httpx.Response(200, json={"key": {"usage": 0, "limit": FREE_MONTHLY_CREDITS}})
        raise httpx.ReadTimeout("模拟超时", request=request)

    transport = httpx.MockTransport(handler)
    quota = TavilyQuota(
        api_key="tvly-test",
        state_path=tmp_path / "tavily_usage.json",
        transport=transport,
        clock=lambda: NOW,
    )
    tool = WebSearchTool(api_key="tvly-test", quota=quota, transport=transport)

    result = await _run(tool, _ctx(tmp_path))

    assert result.is_error
    assert "超时" in result.content[0].text
    assert tool.quota.snapshot().cycle_used == 1


@pytest.mark.asyncio
async def test_cancelled_signal_does_not_search(tmp_path: Path) -> None:
    """已取消：直接拒绝，不花额度。"""

    class _Cancelled(NeverCancelled):
        def is_cancelled(self) -> bool:
            return True

    calls: list[str] = []
    tool = _tool(tmp_path, calls)
    ctx = ToolContext(session_id="test", workspace_root=tmp_path, signal=_Cancelled())

    result = await _run(tool, ctx)

    assert result.is_error
    assert calls == []


# ----------------------------------------------------------------------
# 并发（G39）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_sync_and_keep_the_count(tmp_path: Path) -> None:
    """G39：并发下"校准 + 判定 + 预留"必须是原子的。

    断言两件事：
    1. 两次并发调用都记上（恰好 +2，不丢计数）；
    2. **/usage 只被校准一次** —— 这是锁真正的作用点。
       去掉 asyncio.Lock 后，两个调用会在同一个过期快照上各校准一次（校准次数 2），
       因为"读 synced_at → 发请求 → 写 synced_at"跨了 await。

    注：这里不再断言"恰好一个通过"之类的超发场景——
    预留的读改写在无锁时也是同步的，那样的断言**无法被证伪**，
    属名义门槛（本项目明令：名义门槛比没有门槛更坏）。
    """
    calls: list[str] = []
    state = _seed_state(tmp_path, used=0, synced_at=0.0)  # 过期 → 两个调用都想校准

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        await asyncio.sleep(0)  # 让出控制权，给竞争留出机会
        if request.url.path == "/usage":
            return httpx.Response(200, json={"key": {"usage": 0, "limit": FREE_MONTHLY_CREDITS}})
        return httpx.Response(200, json=_payload())

    transport = httpx.MockTransport(handler)
    quota = TavilyQuota(
        api_key="tvly-test",
        state_path=state,
        transport=transport,
        clock=lambda: NOW,
    )
    tool = WebSearchTool(api_key="tvly-test", quota=quota, transport=transport)
    ctx = _ctx(tmp_path)

    results = await asyncio.gather(_run(tool, ctx), _run(tool, ctx))

    assert all(not r.is_error for r in results)
    assert tool.quota.snapshot().cycle_used == 2
    assert calls.count("/usage") == 1


# ----------------------------------------------------------------------
# 注册策略（G40）：默认不带它
# ----------------------------------------------------------------------


def test_registry_excludes_web_search_unless_enabled() -> None:
    """G40：默认注册表里没有 web_search —— 工具 schema 是常驻成本。"""
    from sigma.sdk import default_registry

    assert "web_search" not in default_registry().names()
    enabled = default_registry(web_search=True, tavily_api_key="tvly-test")
    assert "web_search" in enabled.names()


def test_system_prompt_is_byte_identical_when_disabled() -> None:
    """关掉时提示词逐字节不变——它进常驻区，稳定性是 D4 的硬要求。"""
    from sigma.sdk import SYSTEM_PROMPT, build_system_prompt

    assert build_system_prompt() == SYSTEM_PROMPT
    assert build_system_prompt(web_search=True) != SYSTEM_PROMPT
    assert "web_search" in build_system_prompt(web_search=True)
    assert "web_search" not in SYSTEM_PROMPT
