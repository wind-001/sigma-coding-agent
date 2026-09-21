"""web_search 工具：Tavily 联网搜索。

它解决什么问题
    agent 原来的五个工具全部只作用于本地工作区。遇到"这个库上个月改了 API"、
    "这个报错是什么原因"这类问题，模型只能靠训练记忆猜。

为什么是"可选工具"而不是核心第 6 工具
    architecture 5.1 节的预算表本来就有「工具 schema（+3 可选）按 flag」一栏。
    工具 schema 是常驻成本（进 prompt cache 的那一段），
    没配 key 的机器不该为它付约 150 token。所以：有 key 才注册，--no-web-search 可关。

口径：credits，不是次数
    免费档 1,000 credits/月。basic / fast / ultra-fast 每次 1 credit，
    advanced 每次 2。这条要出现在工具 description 里——
    模型知道贵，才会少用 advanced 档。记账细节见 _tavily_quota.py。

失败一律转成模型可见的结果
    401 / 429 / 超时 / 非 JSON 响应体，全部返回 is_error=True 的 ToolResult，
    绝不向上抛异常（架构 4.3 节第 3 点）——工具抛异常等于把纠错能力关掉。

批次 8 增加的两件事（研究纪律）
    1. **不采信 Tavily 生成的 `answer`**：政策第 1 条明令"只使用 results"。
       做法不是"默认关"，而是**把这个开关删掉**——留着它，模型迟早会打开。
    2. **结果进模型视野之前先过两道硬规则**（`_source_policy.py`）：
       黑名单来源与"摘要里能识别出、且已过期"的结果直接丢弃。
       为什么由代码做、而不是写进提示词：那是**计算型**判断（pi 笔记 13.3），
       不需要理解，可以做得确定且免费；而 LLM 判断会读漏、会心软。
       **丢弃必须可见**：丢了几条会写在给模型看的正文里（不是 details），
       否则模型会以为"网上的资料就这么少"——这是 truncate.py 的同一条教训。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._source_policy import MAX_RESULT_AGE_DAYS, detect_published_at, drop_reason
from sigma_tools._tavily_quota import (
    ADVANCED_COST,
    DEFAULT_COST,
    DEFAULT_TIMEOUT_S,
    TAVILY_BASE_URL,
    QuotaDecision,
    TavilyQuota,
)
from sigma_tools.truncate import truncate_output

if TYPE_CHECKING:
    from collections.abc import Callable


#: 单条结果的摘要上限。与 grep 的「单行截断 240 字符」同一条教训：
#: 整体截断（8 KB）之前必须先截单条，否则第一条结果就能吃光整个输出预算，
#: 后面的结果全部消失。
MAX_SNIPPET_CHARS = 800

#: 判定"服务端说额度用尽"的关键词（Tavily 用 432 表达额度超限）
_QUOTA_KEYWORDS = ("usage limit", "quota", "exceeded", "credit limit", "insufficient credit")


class WebSearchParams(BaseModel):
    """web_search 的参数。"""

    query: str = Field(
        min_length=1,
        max_length=400,
        description="搜索词。具体优于宽泛（'pytest asyncio_mode 配置项' 优于 'pytest'）。",
    )
    max_results: int = Field(default=5, ge=1, le=10, description="返回条数，默认 5。")
    search_depth: Literal["basic", "fast", "ultra-fast", "advanced"] = Field(
        default="basic",
        description=(
            "检索深度：basic/fast/ultra-fast 每次 1 credit，advanced 每次 2 credits。"
            "一般用 basic；只有需要高精度长尾信息时才用 advanced。"
        ),
    )
    topic: Literal["general", "news", "finance"] = Field(
        default="general", description="检索主题。查最新动态用 news。"
    )
    include_old: bool = Field(
        default=False,
        description=(
            "默认 False：早于 2 年的结果会被自动丢弃。"
            "只有查历史沿革 / 不变的原理（如 GIL、JWT 结构）时才设为 True。"
        ),
    )

    @property
    def cost(self) -> int:
        """这次调用花多少 credits。唯一的口径定义处。"""
        return ADVANCED_COST if self.search_depth == "advanced" else DEFAULT_COST


class SearchFailure(Exception):
    """一次搜索失败。

    refundable 决定预留是否退回：

    - True：确定请求没在服务端发生（连接失败 / 401 / 429 / 4xx / 响应不可解析）
    - False：可能已经发生（超时 / 5xx）——宁可本地多记一次，
      漂移交给 /usage 校准。漏记是越界，多记只是保守。

    quota_exhausted：服务端明确说额度用尽，需要把本地账本对齐并禁用。
    """

    def __init__(
        self, message: str, *, refundable: bool = True, quota_exhausted: bool = False
    ) -> None:
        super().__init__(message)
        self.refundable = refundable
        self.quota_exhausted = quota_exhausted


class WebSearchTool(BaseTool):
    """联网搜索。只读工具，允许与其它只读工具并发执行。"""

    name = "web_search"
    description = (
        "联网搜索（Tavily），用于查本地工作区里没有的信息：库的最新用法、报错原因、"
        "版本变更、事实性核对。返回每条结果的标题 + URL + 摘要 + 可识别的发布时间。"
        "低质量来源（内容农场 / 低质 UGC / 广告站）与超过 2 年的旧结果会被自动过滤，"
        "过滤条数会写在结果里——**若结果太少，那是过滤造成的，可以加 include_old 重搜**。"
        "额度按 credits 计：basic/fast/ultra-fast 每次 1 credit，advanced 每次 2 credits，"
        "免费额度 1000 credits/月，用尽后本工具会被禁用。"
        "需要正文时用 web_fetch 精读（它单独计费，每次最多 2 条 URL）。"
    )
    read_only = True

    def __init__(
        self,
        *,
        api_key: str,
        quota: TavilyQuota,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = TAVILY_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._api_key = api_key
        self._quota = quota
        # 注入 transport 是为了离线测试（httpx.MockTransport）——
        # "所有测试不需要 API key" 是既有纪律，新工具不能破例。
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        # 时间预过滤需要一个"今天"。默认取系统时钟，测试注入固定值——
        # 否则"2019 年的结果被丢掉"这类断言会随真实日期漂移（今天红明天绿）。
        self._today: Callable[[], date] = today or (lambda: datetime.now(UTC).date())

    @property
    def quota(self) -> TavilyQuota:
        """账本（CLI 横幅与测试用）。"""
        return self._quota

    @property
    def params(self) -> type[BaseModel]:
        return WebSearchParams

    # ------------------------------------------------------------------

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行搜索。任何失败都不抛异常，一律转成 is_error=True 的结果。"""
        params = cast(WebSearchParams, args)

        if ctx.signal.is_cancelled():
            return ToolResult(
                content=[TextBlock(text="已取消，未发起联网搜索。")],
                details={"query": params.query},
                is_error=True,
            )

        decision = await self._quota.reserve(params.cost)
        if not decision.allowed:
            return self._refusal(params, decision)

        payload: dict[str, Any] = {
            "query": params.query,
            "search_depth": params.search_depth,
            "topic": params.topic,
            "max_results": params.max_results,
            # 刻意**不发** include_answer（政策第 1 条：不要采信 Tavily 生成的答案）。
            # 比"传 false"更硬：参数在协议层就不出现，将来也不会有人顺手打开它。
        }

        try:
            data = await self._post(payload)
        except SearchFailure as exc:
            if exc.quota_exhausted:
                await self._quota.mark_exhausted(str(exc))
            if exc.refundable:
                await self._quota.release(params.cost)
            return ToolResult(
                content=[TextBlock(text=str(exc))],
                details=self._details(params, decision, data=None),
                is_error=True,
            )

        ctx.say(f"web_search: {params.query}")
        # 硬规则先跑：黑名单与过期的结果**不进上下文**（pi 笔记 13.5 Keep Quality Left）。
        kept, filtered = _apply_policy(data, params, now=self._today())
        text, references = _format(params, kept, filtered=filtered)
        truncated = truncate_output(text)
        details = self._details(params, decision, data=data, filtered=filtered)
        details["results"] = references
        details["truncated"] = truncated.truncated
        details["total_lines"] = truncated.total_lines
        details["total_bytes"] = truncated.total_bytes
        return ToolResult(content=[TextBlock(text=truncated.text)], details=details)

    # ------------------------------------------------------------------

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发一次搜索请求。所有异常就地转成 SearchFailure。"""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_s,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/search", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise SearchFailure(
                f"联网搜索超时（{self._timeout_s:.0f}s）：{exc}。"
                f"预留的 {DEFAULT_COST} credit 不退回（请求可能已在服务端发生）。"
                "可以缩小查询范围后重试。",
                refundable=False,
            ) from exc
        except httpx.RequestError as exc:
            raise SearchFailure(
                f"联网搜索连接失败：{type(exc).__name__}: {exc}。"
                "请求未发出，预留的 credit 已退回。"
            ) from exc

        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ")
            if response.status_code >= 500:
                # 5xx：请求到了服务端，可能已被计费。**宁可本地多记一次**，
                # 漂移交给 /usage 校准（漏记是越界，多记只是保守）。
                raise SearchFailure(
                    f"Tavily 服务端错误（HTTP {response.status_code}）：{body}。"
                    "预留的 credit 不退回（请求可能已在服务端发生）。可以稍后重试。",
                    refundable=False,
                )
            if response.status_code in (401, 403):
                raise SearchFailure(
                    f"Tavily 鉴权失败（HTTP {response.status_code}）：{body}。"
                    "请检查 TAVILY_API_KEY 是否正确/是否被吊销。"
                )
            if response.status_code == 429:
                raise SearchFailure(
                    f"Tavily 限流（HTTP {response.status_code}）：{body}。稍后重试。"
                )
            if response.status_code in (432, 433) or _looks_like_quota(body):
                # refundable=False：服务端说用尽了，本地账本一定**落后**于现实，
                # 这时把预留退回去等于让计数永远追不上（mark_exhausted 会被抵消）。
                raise SearchFailure(
                    f"Tavily 报告额度已用尽（HTTP {response.status_code}）：{body}。"
                    "本工具已禁用，请改用本地信息或其它工具。",
                    refundable=False,
                    quota_exhausted=True,
                )
            raise SearchFailure(f"Tavily 返回错误（HTTP {response.status_code}）：{body}")

        try:
            parsed: Any = response.json()
        except ValueError as exc:
            raise SearchFailure(
                f"Tavily 返回的不是 JSON（HTTP {response.status_code}）："
                f"{response.text[:200]}"
            ) from exc
        if not isinstance(parsed, dict):
            raise SearchFailure("Tavily 返回的 JSON 不是对象。")
        return cast("dict[str, Any]", parsed)

    # ------------------------------------------------------------------

    def _refusal(self, params: WebSearchParams, decision: QuotaDecision) -> ToolResult:
        """额度不足时的拒绝结果。文案必须给行动指引。

        与 truncate 的文案同一条教训：只说"不行"会让模型重试同一个调用，
        说清楚"换什么做法"才会改变行为。
        """
        return ToolResult(
            content=[TextBlock(text=decision.reason)],
            details={
                "query": params.query,
                "credits_used": 0,
                "credits_total": decision.used,
                "credits_limit": decision.limit,
                "remaining": decision.remaining,
                "quota_exhausted": True,
            },
            is_error=True,
        )

    def _details(
        self,
        params: WebSearchParams,
        decision: QuotaDecision,
        *,
        data: dict[str, Any] | None,
        filtered: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """给审计与评测看的字段。不进上下文（架构 4.2 节两段式）。"""
        details: dict[str, Any] = {
            "query": params.query,
            "search_depth": params.search_depth,
            "topic": params.topic,
            "credits_used": params.cost,
            "credits_total": decision.used,
            "credits_limit": decision.limit,
            "remaining": decision.remaining,
            "synced_with_server": decision.synced,
            # 被硬规则丢掉的条数。它是"过滤有没有生效"的唯一可查证据——
            # 只看 content 看不出来（丢掉的本来就不在里面）。
            "filtered": filtered or {},
        }
        if data is not None and isinstance(data.get("response_time"), (int, float)):
            details["response_time"] = data["response_time"]
        return details


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _looks_like_quota(body: str) -> bool:
    lowered = body.lower()
    return any(keyword in lowered for keyword in _QUOTA_KEYWORDS)


def _format(
    params: WebSearchParams,
    items: list[dict[str, Any]],
    *,
    filtered: dict[str, int],
) -> tuple[str, list[dict[str, Any]]]:
    """把**已过滤**的结果整成给模型的文本 + 给审计的引用列表。

    每条结果都带 URL 与发布时间是刻意的：模型据此判断"这条能不能用、要不要精读"，
    而用户据此可以核对来源与时效（政策第 10 条：每条事实附 URL + 文档时间）。
    """
    lines: list[str] = []
    references: list[dict[str, Any]] = []

    for index, item in enumerate(items[: params.max_results], start=1):
        title = str(item.get("title") or "(无标题)")
        url = str(item.get("url") or "")
        published = item.get("_published")
        snippet = " ".join(str(item.get("content") or "").split())
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS] + "…"
        time_line = (
            f"    发布时间：{published}"
            if isinstance(published, str)
            else "    发布时间：未识别（无法判断时效——引用时请在回答里说明）"
        )
        lines.append(f"[{index}] {title}\n    {url}\n{time_line}\n    {snippet}")
        references.append(
            {
                "title": title,
                "url": url,
                "score": item.get("score"),
                "published": published if isinstance(published, str) else None,
            }
        )

    if not lines:
        lines.append("没有返回任何结果（或全部被硬规则过滤）。可以换一组更具体的搜索词重试。")

    summary = _filter_summary(filtered)
    if summary:
        lines.append(summary)

    return "\n".join(lines), references


def _apply_policy(
    data: dict[str, Any], params: WebSearchParams, *, now: date
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """跑两道硬规则，返回（保留的结果, 各类丢弃条数）。

    - **黑名单**：命中就丢弃（连"它存在过"都不告诉模型，只报条数）；
    - **时间预过滤**：摘要里能识别出发布时间、且早于 2 年的丢掉；
      识别不出的**保留**——"无法判断"不等于"老旧"，宁可放过，不可错杀；
    - `include_old=True` 时只跑黑名单：查原理 / 历史沿革时旧文章本来是对的。

    被保留的条目会被复制一份并加上 `_published`（ISO 日期或 None）——
    复制是为了不改动调用方传进来的响应对象（它还要进 details 的 response_time）。
    """
    kept: list[dict[str, Any]] = []
    filtered = {"blocked": 0, "stale": 0}
    results = data.get("results")
    if not isinstance(results, list):
        return kept, filtered

    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        snippet = str(item.get("content") or "")
        haystack = f"{title}\n{snippet}"
        reason = drop_reason(
            url, haystack, now=now, include_old=params.include_old
        )
        if reason is not None:
            filtered[reason] += 1
            continue
        found = detect_published_at(haystack, now=now)
        annotated = dict(item)
        annotated["_published"] = found.isoformat() if found is not None else None
        kept.append(annotated)

    return kept, filtered


def _filter_summary(filtered: dict[str, int]) -> str:
    """把丢弃条数写成给模型看的一行。

    **看不见丢弃，就等于没有过滤**：模型只会觉得"网上的资料就这么少"，
    然后拿一条旧资料当现状用——而它没有任何线索去怀疑这个结论。
    （truncate.py 的同一条教训：截断/丢弃必须可见，且要给行动指引。）
    """
    blocked = filtered.get("blocked", 0)
    stale = filtered.get("stale", 0)
    if not blocked and not stale:
        return ""
    parts: list[str] = []
    if blocked:
        parts.append(f"低质量来源 {blocked} 条")
    if stale:
        parts.append(f"超过 {MAX_RESULT_AGE_DAYS} 天的旧结果 {stale} 条")
    hint = (
        "（若确需旧资料，加 include_old=true 重搜）"
        if stale
        else "（换一个更权威的来源）"
    )
    return f"[硬规则已过滤：{'、'.join(parts)}{hint}]"
