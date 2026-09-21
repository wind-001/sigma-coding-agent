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
"""

from __future__ import annotations

from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._tavily_quota import (
    ADVANCED_COST,
    DEFAULT_COST,
    DEFAULT_TIMEOUT_S,
    TAVILY_BASE_URL,
    QuotaDecision,
    TavilyQuota,
)
from sigma_tools.truncate import truncate_output

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
    include_answer: bool = Field(
        default=False, description="是否附带 Tavily 生成的一段直接答案（不做引用校验）。"
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
        "版本变更、事实性核对。返回每条结果的标题 + URL + 摘要片段。"
        "额度按 credits 计：basic/fast/ultra-fast 每次 1 credit，advanced 每次 2 credits，"
        "免费额度 1000 credits/月，用尽后本工具会被禁用。"
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
    ) -> None:
        self._api_key = api_key
        self._quota = quota
        # 注入 transport 是为了离线测试（httpx.MockTransport）——
        # "所有测试不需要 API key" 是既有纪律，新工具不能破例。
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

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
            "include_answer": params.include_answer,
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
        text, references = _format(params, data)
        truncated = truncate_output(text)
        details = self._details(params, decision, data=data)
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
    params: WebSearchParams, data: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    """把响应整成给模型的文本 + 给审计的引用列表。

    每条结果都带 URL 是刻意的：模型据此判断"要不要再取一次"，
    而用户据此可以核对来源。
    """
    lines: list[str] = []

    answer = data.get("answer")
    if params.include_answer and isinstance(answer, str) and answer.strip():
        lines.append("直接答案（未做引用校验）：" + answer.strip())

    references: list[dict[str, Any]] = []
    results = data.get("results")
    if isinstance(results, list):
        for index, item in enumerate(results[: params.max_results], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "(无标题)")
            url = str(item.get("url") or "")
            snippet = " ".join(str(item.get("content") or "").split())
            if len(snippet) > MAX_SNIPPET_CHARS:
                snippet = snippet[:MAX_SNIPPET_CHARS] + "…"
            lines.append(f"[{index}] {title}\n    {url}\n    {snippet}")
            references.append(
                {"title": title, "url": url, "score": item.get("score")}
            )

    if not lines:
        lines.append("没有返回任何结果。可以换一组更具体的搜索词重试。")

    return "\n".join(lines), references
