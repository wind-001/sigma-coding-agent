"""web_fetch 工具的测试：四道硬闸里的三道 + 错误不穿透。

全部离线：`httpx.MockTransport` 注入假响应，不需要 FIRECRAWL_API_KEY、不联网。
"无 key 全绿"是既有纪律（批次 7 详规第 7 节），新工具不能破例。

本文件盯住的四件事（每条都有对应的注入实验，见 `scripts/gate_injection_batch8.py`）
    G42 单次上限 2 条：**在扣额度之前**截断——被截掉的不该产生任何成本；
    G43 黑名单：命中的 URL 不发请求、不扣额度（纵深防御，web_search 之外再来一道）；
    G46 额度硬闸：账本用尽 / 会话闸到顶时**零请求**，且拒绝文案必须给行动指引；
    G47 错误不穿透：401 / 429 / 超时 / `success:false` / 非 JSON 一律转成**模型可见的
        逐条失败结果**（不抛异常），并且**该退的退、不该退的不退**（超时与 403/404 不退）。
        is_error 只留给"调用没执行"（取消）——失败细节在 content 里逐条说，
        这与 web_search（整次调用即失败）口径不同，理由写在 web_fetch.run 的注释里。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from sigma_agent.types import ToolContext
from sigma_ai.base import NeverCancelled
from sigma_tools._firecrawl_quota import (
    FREE_MONTHLY_CREDITS,
    SCRAPE_COST,
    FirecrawlQuota,
)
from sigma_tools.web_fetch import MAX_URLS_PER_CALL, WebFetchTool

NOW = 1000.0
TODAY = "2026-09-21"

GOOD_URL = "https://docs.firecrawl.dev/pricing"
SECOND_URL = "https://docs.firecrawl.dev/features/scrape"


def _ctx(root: Path) -> ToolContext:
    return ToolContext(session_id="test", workspace_root=root, signal=NeverCancelled())


def _payload(
    *,
    markdown: str = "# Pricing\n\nScrape costs 1 credit per page.",
    title: str = "Pricing",
    source_url: str = GOOD_URL,
    published: str | None = "2026-09-19",
    status_code: int = 200,
    success: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    """Firecrawl /scrape 的真实响应形状（2026-09-21 实测）。"""
    metadata: dict[str, Any] = {
        "title": title,
        "sourceURL": source_url,
        "statusCode": status_code,
        "creditsUsed": 1,
    }
    if published is not None:
        metadata["publishedTime"] = published
    body: dict[str, Any] = {"success": success, "data": {"markdown": markdown, "metadata": metadata}}
    if error is not None:
        body["error"] = error
    return body


def _seed_state(
    tmp_path: Path,
    *,
    used: int = 0,
    limit: int = FREE_MONTHLY_CREDITS,
    synced_at: float = NOW,
    disabled: bool = False,
) -> Path:
    """直接落一份账本状态。

    `synced_at=NOW` 且 clock 固定为 NOW 时快照算"新鲜"，
    于是不会产生任何 /team/credit-usage 请求——这正是 G46"零请求"要的初始条件。
    """
    path = tmp_path / "firecrawl_usage.json"
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
    bodies: list[dict[str, Any]] | None = None,
    status: int = 200,
    raw_text: str | None = None,
    usage: tuple[int, int] = (0, FREE_MONTHLY_CREDITS),
    usage_status: int = 200,
) -> Any:
    """按路径分派 /scrape 与 /team/credit-usage。

    每次 /scrape 依次取 `bodies` 里的一条，用来断言"模型点 5 条、实际只抓 2 条"。
    """

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/team/credit-usage"):
            if usage_status >= 400:
                return httpx.Response(usage_status, json={"detail": "usage boom"})
            used, limit = usage
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "remainingCredits": limit - used,
                        "planCredits": limit,
                    },
                },
            )
        if request.url.path.endswith("/scrape"):
            if raw_text is not None:
                return httpx.Response(status, text=raw_text)
            if status >= 400:
                return httpx.Response(status, json={"error": "scrape boom"})
            index = sum(1 for path in calls if path.endswith("/scrape")) - 1
            if bodies is not None and index < len(bodies):
                return httpx.Response(200, json=bodies[index])
            return httpx.Response(200, json=_payload())
        return httpx.Response(404, json={"error": "unexpected path"})

    return handle


def _tool(
    tmp_path: Path,
    calls: list[str],
    *,
    state_path: Path | None = None,
    session_cap: int = 10,
    max_urls: int = MAX_URLS_PER_CALL,
    **handler_kwargs: Any,
) -> WebFetchTool:
    transport = httpx.MockTransport(_handler(calls, **handler_kwargs))
    quota = FirecrawlQuota(
        api_key="fc-test",
        state_path=state_path or (tmp_path / "firecrawl_usage.json"),
        transport=transport,
        clock=lambda: NOW,
    )
    return WebFetchTool(
        api_key="fc-test",
        quota=quota,
        transport=transport,
        max_urls=max_urls,
        session_cap=session_cap,
        today=lambda: __import__("datetime").date.fromisoformat(TODAY),
    )


async def _run(tool: WebFetchTool, ctx: ToolContext, urls: list[str], **kwargs: Any) -> Any:
    args = tool.params.model_validate({"urls": urls, **kwargs})
    return await tool.run(args, ctx)


def _scrape_count(calls: list[str]) -> int:
    return sum(1 for path in calls if path.endswith("/scrape"))


# ----------------------------------------------------------------------
# 正常路径


@pytest.mark.asyncio
async def test_success_returns_markdown_with_source_and_time(tmp_path: Path) -> None:
    calls: list[str] = []
    tool = _tool(tmp_path, calls)

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    text = result.content[0].text
    assert result.is_error is False
    assert "Scrape costs 1 credit per page." in text
    assert GOOD_URL in text
    assert "2026-09-19" in text
    assert "（约 2 天前）" in text
    assert result.details["credits_used"] == SCRAPE_COST
    assert result.details["fetched"] == 1
    assert _scrape_count(calls) == 1


@pytest.mark.asyncio
async def test_reason_is_echoed_into_content(tmp_path: Path) -> None:
    """精读理由要回显——它同时是模型的自我检查（"写不出理由就别抓"）。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls)

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL], reason="确认计费口径")

    assert "确认计费口径" in result.content[0].text
    assert result.details["reason"] == "确认计费口径"


# ----------------------------------------------------------------------
# G42 单次上限


@pytest.mark.asyncio
async def test_g42_only_top_two_urls_are_fetched(tmp_path: Path) -> None:
    """点 5 条 URL → 只发 2 次请求；被截掉的 3 条**明确告知**模型。

    截断发生在扣额度之前，所以另外 3 条不产生任何成本。
    """
    calls: list[str] = []
    tool = _tool(tmp_path, calls)
    urls = [f"https://example.com/page{i}" for i in range(1, 6)]

    result = await _run(tool, _ctx(tmp_path), urls)

    assert _scrape_count(calls) == 2, "单次上限失效：多发的请求就是多花的 credit"
    assert result.details["requested"] == 5
    assert len(result.details["dropped_urls"]) == 3
    assert result.details["credits_used"] == 2 * SCRAPE_COST
    # 丢弃必须回到模型眼前，否则它会以为"网上只有 2 条资料"
    assert "未取 3 条" in result.content[0].text
    assert "再调用一次" in result.content[0].text


@pytest.mark.asyncio
async def test_g42_truncation_happens_before_charging(tmp_path: Path) -> None:
    """额度只够 1 条时：点 3 条 URL 只发 1 次请求，剩下的明确停在额度上。"""
    calls: list[str] = []
    state = _seed_state(tmp_path, used=FREE_MONTHLY_CREDITS - 1)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/team/credit-usage"):
            # 真实服务端的用量**包含我们已经发出的每一次抓取**——
            # 回固定值的话，第二次校准会把"已用尽"抹掉，那是假环境的假故障
            # （同批次 7 的教训：假服务端必须跟着记账）。
            used = FREE_MONTHLY_CREDITS - 1 + _scrape_count(calls)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "remainingCredits": FREE_MONTHLY_CREDITS - used,
                        "planCredits": FREE_MONTHLY_CREDITS,
                    },
                },
            )
        return httpx.Response(200, json=_payload())

    transport = httpx.MockTransport(handler)
    quota = FirecrawlQuota(
        api_key="fc-test",
        state_path=state,
        transport=transport,
        clock=lambda: NOW,
    )
    tool = WebFetchTool(
        api_key="fc-test",
        quota=quota,
        transport=transport,
        today=lambda: __import__("datetime").date.fromisoformat(TODAY),
    )

    result = await _run(
        tool, _ctx(tmp_path), ["https://a.com/1", "https://a.com/2", "https://a.com/3"]
    )

    assert _scrape_count(calls) == 1
    assert result.details["credits_used"] == SCRAPE_COST
    assert "已停止" in result.content[0].text


# ----------------------------------------------------------------------
# G43 黑名单（纵深防御）


@pytest.mark.asyncio
async def test_g43_blocked_url_costs_nothing(tmp_path: Path) -> None:
    """黑名单 URL：**零请求、零扣费**，且返回可读的拒绝理由。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls)

    result = await _run(tool, _ctx(tmp_path), ["https://blog.csdn.net/x/article/details/1"])

    assert _scrape_count(calls) == 0, "黑名单没拦住：请求发出去了，credit 也花了"
    assert result.details["credits_used"] == 0
    assert result.details["blocked"] == 1
    assert result.details["results"][0]["status"] == "blocked"
    assert "黑名单" in result.content[0].text
    assert "换一个来源" in result.content[0].text


@pytest.mark.asyncio
async def test_g43_authoritative_source_is_never_blocked(tmp_path: Path) -> None:
    """误杀比漏过更坏：stackoverflow 必须能抓。

    这条用例守的是 `NEVER_BLOCK` 的优先级——黑名单"只许漏，不许误杀"。
    """
    calls: list[str] = []
    tool = _tool(tmp_path, calls, bodies=[_payload(source_url="https://stackoverflow.com/q/1")])

    result = await _run(tool, _ctx(tmp_path), ["https://stackoverflow.com/questions/1/how"])

    assert _scrape_count(calls) == 1
    assert result.details["blocked"] == 0
    assert result.details["credits_used"] == SCRAPE_COST


@pytest.mark.asyncio
async def test_g43_blocked_and_good_urls_mix(tmp_path: Path) -> None:
    """一条被拦、一条正常：正常的那条不受影响，计数各自准确。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, bodies=[_payload(source_url=GOOD_URL)])

    result = await _run(
        tool, _ctx(tmp_path), ["https://www.geeksforgeeks.org/x/", GOOD_URL]
    )

    assert _scrape_count(calls) == 1
    assert result.details["blocked"] == 1
    assert result.details["fetched"] == 1
    assert result.details["credits_used"] == SCRAPE_COST


# ----------------------------------------------------------------------
# G46 额度硬闸


@pytest.mark.asyncio
async def test_g46_exhausted_ledger_sends_no_request(tmp_path: Path) -> None:
    """账本用尽 → **零 /scrape 请求**，返回 is_error + 行动指引。"""
    calls: list[str] = []
    state = _seed_state(tmp_path, used=FREE_MONTHLY_CREDITS)
    # 同上：服务端必须与本地一致，否则临近上限的强制校准会把 disabled 抹掉
    tool = _tool(
        tmp_path,
        calls,
        state_path=state,
        usage=(FREE_MONTHLY_CREDITS, FREE_MONTHLY_CREDITS),
    )

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert _scrape_count(calls) == 0, "额度已用尽仍发出请求——闸门形同不存在"
    assert result.is_error is False, "额度用尽是正常的业务拒绝，不是工具故障"
    assert result.details["credits_used"] == 0
    assert "已停止" in result.content[0].text
    assert "摘要" in result.content[0].text


@pytest.mark.asyncio
async def test_g46_disabled_ledger_sends_no_request(tmp_path: Path) -> None:
    """账本被标记 disabled（服务端曾说用尽）→ 同样零请求。"""
    calls: list[str] = []
    state = _seed_state(tmp_path, disabled=True)
    tool = _tool(tmp_path, calls, state_path=state)

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert _scrape_count(calls) == 0
    assert result.details["credits_used"] == 0


@pytest.mark.asyncio
async def test_g46_session_cap_stops_second_call(tmp_path: Path) -> None:
    """会话闸到顶后，**第二次调用起零请求**。

    它防的是"一个跑飞的 loop 在一轮里把整月额度烧掉"——
    全局月度闸要到月底用尽才停，那时已经太晚了。
    """
    calls: list[str] = []
    tool = _tool(tmp_path, calls, session_cap=2)
    ctx = _ctx(tmp_path)

    first = await _run(tool, ctx, ["https://a.com/1", "https://a.com/2"])
    assert first.details["credits_used"] == 2
    assert tool.session_used == 2

    before = _scrape_count(calls)
    second = await _run(tool, ctx, ["https://a.com/3", "https://a.com/4"])

    assert _scrape_count(calls) == before, "会话闸没拦住，又发了请求"
    assert second.details["credits_used"] == 0
    assert "会话" in second.content[0].text
    assert "摘要" in second.content[0].text


@pytest.mark.asyncio
async def test_g46_session_counter_increments_per_document(tmp_path: Path) -> None:
    """会话计数按**条**算，不是按调用次数算。

    按调用算的话，一次给 2 条 URL 只记 1，会话闸的价值直接减半。
    """
    calls: list[str] = []
    tool = _tool(tmp_path, calls, session_cap=3)

    await _run(tool, _ctx(tmp_path), ["https://a.com/1", "https://a.com/2"])
    assert tool.session_used == 2

    await _run(tool, _ctx(tmp_path), ["https://a.com/3"])
    assert tool.session_used == 3


# ----------------------------------------------------------------------
# G47 错误不穿透


@pytest.mark.asyncio
async def test_g47_unauthenticated_is_error_and_refunds(tmp_path: Path) -> None:
    """401：请求被拒、没产生结果 → 预留退回。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, status=401)

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.is_error is False
    assert result.details["credits_used"] == 0, "401 不该计费"
    assert "401" in result.content[0].text


@pytest.mark.asyncio
async def test_g47_rate_limited_is_error_and_refunds(tmp_path: Path) -> None:
    """429：限流，服务端未处理 → 退回。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, status=429)

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.is_error is False
    assert result.details["credits_used"] == 0
    assert "429" in result.content[0].text


@pytest.mark.asyncio
async def test_g47_quota_exhausted_disables_and_does_not_refund(tmp_path: Path) -> None:
    """402：服务端说用尽 → **不退**（本地账本一定落后于现实），并禁用本工具。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, status=402)

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.is_error is False
    assert result.details["credits_used"] == SCRAPE_COST, "服务端说用尽反而退额度，计数再也不会收敛"
    assert tool.quota.snapshot().disabled is True
    assert "用尽" in result.content[0].text


@pytest.mark.asyncio
async def test_g47_success_false_refunds(tmp_path: Path) -> None:
    """`success: false`：官方计费表写明"没有结果的抓取不计费" → 退回。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, bodies=[_payload(success=False, error="Failed to scrape")])

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.is_error is False
    assert result.details["credits_used"] == 0
    assert "未产生费用" in result.content[0].text


@pytest.mark.asyncio
async def test_g47_page_404_still_charges(tmp_path: Path) -> None:
    """页面本身 404：官方计费表写明**仍然计 1 credit** → 不退，但如实告诉模型。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, bodies=[_payload(status_code=404)])

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.is_error is False
    assert result.details["credits_used"] == SCRAPE_COST, "403/404 计费口径写反了"
    assert "404" in result.content[0].text
    assert "不退回" in result.content[0].text


@pytest.mark.asyncio
async def test_g47_non_json_body_is_error(tmp_path: Path) -> None:
    """非 JSON 响应（例如被网关换成 HTML 错误页）→ is_error 结果，不抛异常。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, raw_text="<html>502 Bad Gateway</html>")

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.is_error is False
    assert result.details["credits_used"] == 0
    assert "不是 JSON" in result.content[0].text


@pytest.mark.asyncio
async def test_g47_empty_body_refunds(tmp_path: Path) -> None:
    """抓到了但正文为空（反爬/纯前端渲染）→ 不计费。"""
    calls: list[str] = []
    tool = _tool(tmp_path, calls, bodies=[_payload(markdown="   ")])

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.is_error is False
    assert result.details["credits_used"] == 0
    assert "没有返回正文" in result.content[0].text


@pytest.mark.asyncio
async def test_g47_one_failure_does_not_kill_the_other(tmp_path: Path) -> None:
    """两条 URL 里第一条失败，第二条仍要抓——单条失败不该拖垮整批。"""
    calls: list[str] = []
    tool = _tool(
        tmp_path,
        calls,
        bodies=[_payload(success=False, error="blocked"), _payload(source_url=SECOND_URL)],
    )

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL, SECOND_URL])

    assert _scrape_count(calls) == 2
    assert result.details["fetched"] == 1
    assert result.details["credits_used"] == SCRAPE_COST
    assert "未产生费用" in result.content[0].text
    assert "Scrape costs 1 credit per page." in result.content[0].text


# ----------------------------------------------------------------------
# stale 标注（Q5：标注不丢正文）


@pytest.mark.asyncio
async def test_old_page_is_flagged_not_dropped(tmp_path: Path) -> None:
    """过旧页面：正文**照常返回**（credit 已经花了），但在开头显著标注。

    按政策原样丢弃等于钱花了、信息也没拿到——模型只会再换一条 URL 抓，烧掉更多额度。
    """
    calls: list[str] = []
    tool = _tool(tmp_path, calls, bodies=[_payload(published="2019-04-11")])

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.is_error is False
    assert result.details["credits_used"] == SCRAPE_COST
    assert result.details["results"][0]["stale"] is True
    assert result.details["results"][0]["published"] == "2019-04-11"
    text = result.content[0].text
    assert "⚠" in text
    assert "已超出时效范围" in text
    assert "Scrape costs 1 credit per page." in text, "旧正文被丢掉了——credit 白花"


@pytest.mark.asyncio
async def test_unknown_publish_time_is_reported_not_assumed_fresh(tmp_path: Path) -> None:
    """认不出发布时间：**不猜**，如实告诉模型无法判断时效。"""
    calls: list[str] = []
    tool = _tool(
        tmp_path,
        calls,
        bodies=[_payload(markdown="# Notes\n\nNo dates anywhere here.", published=None)],
    )

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.details["results"][0]["stale"] is False
    assert result.details["results"][0]["published"] is None
    assert "未识别" in result.content[0].text


@pytest.mark.asyncio
async def test_metadata_publish_time_beats_body_date(tmp_path: Path) -> None:
    """`metadata.publishedTime` 优先于正文日期。

    正文里常有大量历史引用（"2023 年引入的 API"），
    metadata 那个才是**这个页面自己的**发布时间。
    """
    calls: list[str] = []
    tool = _tool(
        tmp_path,
        calls,
        bodies=[_payload(markdown="本文基于 2019-01-01 的接口，已更新。", published="2026-09-19")],
    )

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.details["results"][0]["published"] == "2026-09-19"
    assert result.details["results"][0]["stale"] is False


# ----------------------------------------------------------------------
# 截断（超长正文不该吃掉整轮上下文）


@pytest.mark.asyncio
async def test_long_body_is_truncated(tmp_path: Path) -> None:
    """200 KB 正文 → 单条上限截断 + 整体截断，`details.truncated` 如实上报。"""
    calls: list[str] = []
    huge = "# Long\n\n" + "x" * 200_000
    tool = _tool(tmp_path, calls, bodies=[_payload(markdown=huge)])

    result = await _run(tool, _ctx(tmp_path), [GOOD_URL])

    assert result.details["truncated"] is True
    assert result.details["total_bytes"] > 8000
    assert len(result.content[0].text) < 9000
    assert "单条上限" in result.content[0].text


@pytest.mark.asyncio
async def test_cancelled_signal_sends_no_request(tmp_path: Path) -> None:
    """已取消：零请求、零扣费。"""

    class _Cancelled(NeverCancelled):
        def is_cancelled(self) -> bool:
            return True

    calls: list[str] = []
    tool = _tool(tmp_path, calls)
    ctx = ToolContext(session_id="test", workspace_root=tmp_path, signal=_Cancelled())

    result = await _run(tool, ctx, [GOOD_URL])

    assert _scrape_count(calls) == 0
    assert result.is_error is True


# ----------------------------------------------------------------------
# 账本子类（FirecrawlQuota._parse_usage 的方向不能反）


def test_parse_usage_converts_remaining_to_used(tmp_path: Path) -> None:
    """Firecrawl 给的是**剩余**额度，与 Tavily 的"已用量"相反。

    少了这一步换算，账本会安静地反向漂移（把剩余当已用），
    症状是"额度用了一点就不能用了"，很难指向根因。
    """
    quota = FirecrawlQuota(api_key="fc-test", state_path=tmp_path / "u.json")

    assert quota._parse_usage({"data": {"remainingCredits": 999, "planCredits": 1000}}) == (1, 1000)
    assert quota._parse_usage({"data": {"remainingCredits": 0, "planCredits": 1000}}) == (1000, 1000)
    assert quota._parse_usage({"data": {"remainingCredits": 5, "planCredits": 3}}) == (0, 3)
    assert quota._parse_usage({"data": {}}) is None
    assert quota._parse_usage({}) is None
    assert quota._parse_usage({"data": {"remainingCredits": "x", "planCredits": 1000}}) is None
