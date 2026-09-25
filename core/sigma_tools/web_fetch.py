"""web_fetch 工具：Firecrawl 精读指定 URL 的正文。

它解决什么问题
    `web_search` 只给摘要。政策里"只有摘要不足以确认关键结论时才精读"这一条，
    在批次 7 的工具集里**没有对应的动作**——模型只能反复换搜索词碰运气，
    每次都在花 Tavily 的 credit。

为什么是独立工具，而不是塞进 web_search
    1. 两者是**不同的原语**（pi 笔记 2 节：primitives, not features）：
       "搜索"与"精读"的失败模式、计费口径、输出形状都不一样；
    2. 判断"要不要精读、精读哪条"是**推断型**工作，应该留在模型手里（13.3）；
       代码只做那件无法判断的事——**设上限**。
    3. 混在一个工具里，一次调用会同时花 Tavily 的 credit 与 Firecrawl 的 credit，
       `details` 无法分账，"这次调研花了多少"就再也答不上来。

四道硬闸里本工具占三道（另一道在 web_search 里）
    - **单次上限 2 条 URL**：模型给 5 条也只在扣额度**之前**取前 2 条；
    - **黑名单**：命中的 URL 不发请求、不扣额度；
    - **额度计数**：全局月度（账本）+ 进程内会话闸（本文件）。

为什么"截断"而不是"报错"
    报错会让模型重试同一批 URL——同样的 2 条被再抓一次，白花 2 credits，
    而它并不知道第二次也会被截断。截断 + 说清"还剩哪几条没取"才会改变它的下一步。
    这是 `truncate.py` 的同一条教训：**文案必须给出行动指引**。

失败一律转成模型可见的结果
    401 / 429 / 402 / 超时 / `success: false` / 非 JSON，全部返回 is_error=True 的
    ToolResult，绝不向上抛异常（架构 4.3 节第 3 点）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx
from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._firecrawl_quota import (
    API_TIMEOUT_MS,
    FETCH_TIMEOUT_S,
    MAX_CACHE_AGE_MS,
    SCRAPE_COST,
    SESSION_FETCH_CAP,
    FirecrawlQuota,
)
from sigma_tools._credit_ledger import QuotaDecision
from sigma_tools._source_policy import (
    MAX_DOC_AGE_DAYS,
    age_days,
    describe_age,
    detect_published_at,
    is_blocked_url,
    is_stale,
)
from sigma_tools.truncate import truncate_output

if TYPE_CHECKING:
    from collections.abc import Callable

#: **政策指定值**：单次最多抓 2 条 URL。写成常量，不散落在代码里。
MAX_URLS_PER_CALL = 2

#: 单条正文的字符上限（只抓 1 条时）：放宽到接近整体 8 KB 预算——
#: 精读的意义就是拿正文，单页时没必要提前收手；整体截断（truncate_output）兜底。
#:
#: ⚠ 这上限是**含**"本页被截断"说明文案的（见 ``_render``）——不是正文自己独享。
MAX_BODY_CHARS = 8000

#: 多条时的单条正文上限：**均分预算**，保证第二条不会消失。
#: 与 MAX_SNIPPET_CHARS（800）同一条教训：第一条不许吃光整个输出预算。
SHARED_BODY_CHARS = 3500

#: 判定"服务端说额度用尽"的关键词（Firecrawl 用 402）
_QUOTA_KEYWORDS = ("credit", "quota", "usage limit", "exceeded", "insufficient")


class WebFetchParams(BaseModel):
    """web_fetch 的参数。"""

    urls: list[str] = Field(
        min_length=1,
        max_length=10,
        description=(
            "要精读的 URL 列表，**按重要性排序**。框架硬上限是每次 2 条，"
            "只取前 2 条；确实还需要更多时另发一次调用。"
        ),
    )
    reason: str = Field(
        default="",
        max_length=200,
        description=(
            "为什么要精读它（一句话）。用于审计与自检——"
            "写不出理由，通常就说明摘要已经够了，不该抓。"
        ),
    )


class FetchFailure(Exception):
    """一次抓取失败。

    refundable 决定预留是否退回：

    - True：确定请求没在服务端产生结果（连接失败 / 401 / 429 / 4xx / `success: false`）
    - False：**可能已经计费**（超时 / 5xx / 页面本身 403·404）。
      官方计费表写着"返回 403/404 的页面仍然收 1 credit"，
      所以这一类不退——漏记是越界，多记只是保守，漂移交给校准收敛。

    quota_exhausted：服务端明确说额度用尽，需要把本地账本对齐并禁用。
    """

    def __init__(
        self, message: str, *, refundable: bool = True, quota_exhausted: bool = False
    ) -> None:
        super().__init__(message)
        self.refundable = refundable
        self.quota_exhausted = quota_exhausted


class WebFetchTool(BaseTool):
    """精读网页正文。只读工具，允许与其它只读工具并发执行。"""

    name = "web_fetch"
    description = (
        "精读指定页面的正文（Firecrawl，Markdown）。用于搜索摘要不足以确认关键结论时。"
        "**每次最多精读 2 条 URL**（框架硬上限，多给的会被丢弃并告知）；"
        "一次抓取 = 1 credit，免费额度 1000 credits/月，用尽后本工具被禁用。"
        "低质量来源（内容农场 / 低质 UGC / 广告站）会被直接拒绝，不发请求也不扣额度。"
        "输出含来源 URL 与页面发布时间；发布时间超出时效范围时会显著标注。"
        "**摘要够用时不要调用它**——精读是要花额度的。"
    )
    read_only = True

    def __init__(
        self,
        *,
        api_key: str,
        quota: FirecrawlQuota,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        timeout_s: float = FETCH_TIMEOUT_S,
        max_urls: int = MAX_URLS_PER_CALL,
        session_cap: int = SESSION_FETCH_CAP,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._api_key = api_key
        self._quota = quota
        # 注入 transport 是为了离线测试（httpx.MockTransport）：
        # "所有测试不需要 API key" 是既有纪律，新工具不能破例。
        self._transport = transport
        self._base_url = (base_url or FirecrawlQuota.DEFAULT_BASE_URL).rstrip("/")
        self._timeout_s = timeout_s
        self._max_urls = max_urls
        self._session_cap = session_cap
        # 会话内的计数。它防的不是"这个月用超了"（那是账本），
        # 而是"一个跑飞的 loop 在这一轮里把整月额度烧掉"。
        self._session_used = 0
        self._today: Callable[[], date] = today or (lambda: datetime.now(UTC).date())

    @property
    def quota(self) -> FirecrawlQuota:
        """账本（CLI 横幅与测试用）。"""
        return self._quota

    @property
    def session_used(self) -> int:
        """本会话已经抓了多少条（测试与评测用）。"""
        return self._session_used

    @property
    def params(self) -> type[BaseModel]:
        return WebFetchParams

    # ------------------------------------------------------------------

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行精读。任何失败都不抛异常，一律转成 is_error=True 的结果。"""
        params = cast(WebFetchParams, args)

        if ctx.signal.is_cancelled():
            return ToolResult(
                content=[TextBlock(text="已取消，未发起精读。")],
                details={"urls": params.urls, "credits_used": 0},
                is_error=True,
            )

        # 闸一：**在扣额度之前**截断。被截掉的 URL 不该产生任何成本。
        wanted, dropped = params.urls[: self._max_urls], params.urls[self._max_urls:]

        # 正文预算：只抓 1 条时放宽（尽量多拿正文）；
        # 抓多条时均分，保证第二条不会消失（同 MAX_SNIPPET_CHARS 的教训）。
        budget = MAX_BODY_CHARS if len(wanted) == 1 else SHARED_BODY_CHARS

        sections: list[str] = []
        records: list[dict[str, Any]] = []
        charged = 0
        blocked = 0
        stopped = ""
        decision: QuotaDecision | None = None

        for index, url in enumerate(wanted, start=1):
            # 闸二：黑名单。不发请求、不扣额度（纵深防御：web_search 已经滤过一遍，
            # 但模型也可能自己拼一个 URL 出来）。
            if is_blocked_url(url):
                blocked += 1
                sections.append(
                    f"[跳过] {url}\n"
                    "    该域名在低质量来源黑名单里（内容农场 / 低质 UGC / 广告站）——"
                    "未发起请求、未消耗额度。请换一个来源。"
                )
                records.append({"url": url, "status": "blocked"})
                continue

            # 闸三：会话上限。
            if self._session_used >= self._session_cap:
                stopped = (
                    f"本次会话的精读上限（{self._session_cap} 条）已用完，**不再发起抓取**。"
                    "请改用已有的搜索结果摘要，或直接告诉用户信息不足。"
                )
                break

            # 闸三之二：额度账本（全局月度）。
            decision = await self._quota.reserve(SCRAPE_COST)
            if not decision.allowed:
                stopped = decision.reason
                break

            try:
                data = await self._post(url)
                section, record = self._render(url, data, index, budget)
            except FetchFailure as exc:
                if exc.quota_exhausted:
                    await self._quota.mark_exhausted(str(exc))
                if exc.refundable:
                    await self._quota.release(SCRAPE_COST)
                else:
                    # 不退 = 这次请求可能已经在服务端计费（超时 / 5xx / 页面 403·404）。
                    # `credits_used` 记的是"这次调用实际花了多少"，
                    # 不是"成功抓到几条"——两者分开，评测才查得出钱花在哪。
                    charged += SCRAPE_COST
                sections.append(f"[失败] {url}\n    {exc}")
                records.append({"url": url, "status": "error", "reason": str(exc)})
                continue

            charged += SCRAPE_COST
            self._session_used += 1
            sections.append(section)
            records.append(record)
            ctx.say(f"web_fetch: {url}")

        text = self._compose(params, sections, dropped=dropped, blocked=blocked, stopped=stopped)
        truncated = truncate_output(text)
        details = self._details(
            params,
            records,
            decision,
            dropped=dropped,
            blocked=blocked,
            charged=charged,
        )
        # 「有没有被截断」是**两级**的：把握整体的 truncate_output 会先砍一刀，
        # 而每条正文可能早在 ``_render`` 里就被自己的上限砍过了。
        # 只报前者会漏掉"工具自己已经砍过"的情况（details 是评测与复盘的事实来源）。
        body_cut = any(record.get("truncated") for record in records)
        details["truncated"] = truncated.truncated or body_cut
        details["total_bytes"] = truncated.total_bytes
        # is_error 的口径（与 web_search 不同，原因要说清楚）：
        # web_fetch 是"逐条报告"的工具——每条 URL 的成败都写在 content 里
        # （[失败] / [跳过] / [已停止] 各说各的），模型据此**逐条**纠错；
        # is_error 在 loop 里表示"这次调用没执行"，只有取消配得上它。
        # 把 401 / 额度拒绝标成 is_error 的后果，是渲染与评测把它们当成工具崩溃，
        # 而"配额用尽"是正常业务状态，不是故障。
        return ToolResult(content=[TextBlock(text=truncated.text)], details=details)

    async def _post(self, url: str) -> dict[str, Any]:
        """发一次抓取请求。所有异常就地转成 FetchFailure。"""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "url": url,
            "formats": ["markdown"],
            # onlyMainContent：只取正文，去掉导航与页脚。**这不是为了好看**——
            # 上下文预算有限，导航栏对结论零贡献，却要按 token 付费。
            "onlyMainContent": True,
            # blockAds 默认就是 true，这里显式写出来：它是本工具的来源质量立场的一部分，
            # 不该依赖对方某天的默认值。
            "blockAds": True,
            # 请求体里的超时（毫秒）**必须小于 httpx 的超时**：
            # 让服务端先放弃并返回结构化错误，好过我们这边超时后按纪律不退额度。
            "timeout": API_TIMEOUT_MS,
            # 48 小时内的缓存可以直接用：省一次真实抓取的时间，
            # 又保证精读到的不是一周前的旧页面（时效性是这个批次的主题）。
            "maxAge": MAX_CACHE_AGE_MS,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_s,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/scrape", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise FetchFailure(
                f"精读超时（{self._timeout_s:.0f}s）：{exc}。"
                f"预留的 {SCRAPE_COST} credit 不退回（请求可能已在服务端发生）。"
                "可以换一个更小的页面，或稍后重试。",
                refundable=False,
            ) from exc
        except httpx.RequestError as exc:
            raise FetchFailure(
                f"精读连接失败：{type(exc).__name__}: {exc}。"
                "请求未发出，预留的 credit 已退回。"
            ) from exc

        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ")
            if response.status_code >= 500:
                raise FetchFailure(
                    f"Firecrawl 服务端错误（HTTP {response.status_code}）：{body}。"
                    "预留的 credit 不退回（请求可能已在服务端发生）。可以稍后重试。",
                    refundable=False,
                )
            if response.status_code in (401, 403):
                raise FetchFailure(
                    f"Firecrawl 鉴权失败（HTTP {response.status_code}）：{body}。"
                    "请检查 FIRECRAWL_API_KEY 是否正确/是否被吊销。"
                )
            if response.status_code == 429:
                raise FetchFailure(
                    f"Firecrawl 限流（HTTP {response.status_code}）：{body}。稍后重试。"
                )
            if response.status_code == 402 or _looks_like_quota(body):
                # 与批次 7 的 432 同理：服务端说用尽了，本地账本一定**落后**于现实，
                # 这时把预留退回去等于让计数永远追不上（mark_exhausted 会被抵消）。
                raise FetchFailure(
                    f"Firecrawl 报告额度已用尽（HTTP {response.status_code}）：{body}。"
                    "本工具已禁用，请改用已有的搜索结果或其它工具。",
                    refundable=False,
                    quota_exhausted=True,
                )
            raise FetchFailure(f"Firecrawl 返回错误（HTTP {response.status_code}）：{body}")

        try:
            parsed: Any = response.json()
        except ValueError as exc:
            raise FetchFailure(
                f"Firecrawl 返回的不是 JSON（HTTP {response.status_code}）："
                f"{response.text[:200]}"
            ) from exc
        if not isinstance(parsed, dict):
            raise FetchFailure("Firecrawl 返回的 JSON 不是对象。")
        return cast("dict[str, Any]", parsed)

    def _render(
        self, url: str, data: dict[str, Any], index: int, budget: int
    ) -> tuple[str, dict[str, Any]]:
        """把一次成功响应整成给模型看的一段 + 给审计的一条记录。"""
        if data.get("success") is not True:
            # 官方计费表："没有结果的抓取不计费" → 这一段是可退回的
            reason = data.get("error")
            message = str(reason)[:200] if reason else "success 不是 true"
            raise FetchFailure(
                f"Firecrawl 未能抓取该页面：{message}。未产生费用，预留已退回。请换一个来源。"
            )

        payload = data.get("data")
        if not isinstance(payload, dict):
            raise FetchFailure(
                "Firecrawl 返回里没有 data 字段（抓取失败）。未产生费用，预留已退回。"
            )

        metadata_raw = payload.get("metadata")
        metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}

        status = metadata.get("statusCode")
        if isinstance(status, int) and status >= 400:
            raise FetchFailure(
                f"该页面返回 HTTP {status}。官方计费表写明这种页面**仍然计 1 credit**，"
                "所以这次预留不退回；请换一个来源。",
                refundable=False,
            )

        markdown = payload.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise FetchFailure(
                "该页面没有返回正文（可能被反爬拦住，或页面是纯前端渲染）。"
                "未产生费用，预留已退回。请换一个来源。"
            )

        title = metadata.get("title")
        title_text = title.strip() if isinstance(title, str) and title.strip() else "(无标题)"
        source = metadata.get("sourceURL")
        source_text = source.strip() if isinstance(source, str) and source.strip() else url

        now = self._today()
        published = self._published_of(metadata, markdown, now=now)
        stale = False
        if published is None:
            time_line = "    发布时间：未识别（无法判断时效——引用时请在回答里说明这一点）"
        else:
            days = age_days(published, now)
            time_line = f"    发布时间：{published.isoformat()}（{describe_age(days)}）"
            stale = is_stale(published, now=now, max_age_days=MAX_DOC_AGE_DAYS)
            if stale:
                time_line += (
                    f"\n    ⚠ 该页面已超出时效范围（>{MAX_DOC_AGE_DAYS} 天）："
                    "**引用它之前先找更新的来源**，并在回答里说明结论来自旧文档。"
                )

        body = markdown.strip()
        cut = len(body) > budget
        if cut:
            notice = (
                f"[... 本页正文较长，此处只保留前 {budget} 字符"
                "（框架单条上限；**重复抓取同一条不会拿到更多**）。"
                "如需更聚焦的内容，请改用 web_search 缩小范围。]\n"
            )
            # 说明放在正文**前面**，不是后面——这不是排版偏好，是有实测凭据的：
            # 挂在长正文后面时，段落总字节（~8300）会超过 truncate_output 的
            # MAX_BYTES（8192），说明恰好是**最后一行**，整体截断第一个切的就是它
            # （2026-09-24 实测：'单条上限' 不在输出里）——而它是唯一阻止模型
            # "徒劳重抓一次、又扣一份额度"的信息。放在正文前面，它永远在
            # 头部预算之内，不会被任何一刀切掉。成本控制是硬约束。
            body = notice + body[:budget]

        section = f"[{index}] {title_text}\n    {source_text}\n{time_line}\n{body}"
        record: dict[str, Any] = {
            "url": source_text,
            "title": title_text,
            "status": "ok",
            "published": published.isoformat() if published is not None else None,
            "stale": stale,
            "credits": SCRAPE_COST,
            # 自查需要具备的能力："这条的内容被我砍过"要在**审计字段**里说出来，
            # 不能只指望模型从正文里那句话读出来——评测与复盘看的是 details。
            "truncated": cut,
        }
        return section, record

    def _published_of(
        self, metadata: dict[str, Any], markdown: str, *, now: date
    ) -> date | None:
        """先信 `metadata.publishedTime`，再退回正文里的日期。

        顺序不能反：很多页面在正文里引用大量历史日期（"2023 年引入的 API"），
        而 metadata 里那个才是**这个页面自己的**发布时间。
        """
        raw = metadata.get("publishedTime")
        if isinstance(raw, str) and raw.strip():
            found = detect_published_at(raw, now=now)
            if found is not None:
                return found
        return detect_published_at(markdown[:4000], now=now)

    def _compose(
        self,
        params: WebFetchParams,
        sections: list[str],
        *,
        dropped: list[str],
        blocked: int,
        stopped: str,
    ) -> str:
        """拼给模型看的正文。**所有被截断/丢弃/中止的事实都必须出现在这里。**"""
        lines: list[str] = []
        if params.reason.strip():
            lines.append(f"精读理由（模型自述）：{params.reason.strip()}")

        if sections:
            lines.extend(sections)
        else:
            lines.append("本次没有取到任何正文（全部被跳过或拒绝）。")

        if dropped:
            lines.append(
                f"[硬上限：单次最多 {self._max_urls} 条 URL，本次未取 {len(dropped)} 条："
                + "、".join(dropped)
                + "。若确实还需要，请再调用一次 web_fetch。]"
            )
        if blocked:
            lines.append(f"[已按来源黑名单跳过 {blocked} 条（未消耗额度）——换一个来源再试。]")
        if stopped:
            lines.append(f"[已停止：{stopped}]")
        return "\n".join(lines)

    def _details(
        self,
        params: WebFetchParams,
        records: list[dict[str, Any]],
        decision: QuotaDecision | None,
        *,
        dropped: list[str],
        blocked: int,
        charged: int,
    ) -> dict[str, Any]:
        """给审计与评测看的字段。不进上下文（架构 4.2 节两段式）。"""
        details: dict[str, Any] = {
            "reason": params.reason,
            "requested": len(params.urls),
            "fetched": sum(1 for record in records if record.get("status") == "ok"),
            "dropped_urls": dropped,
            "blocked": blocked,
            "credits_used": charged,
            "session_used": self._session_used,
            "session_cap": self._session_cap,
            "results": records,
        }
        if decision is not None:
            details["credits_total"] = decision.used
            details["credits_limit"] = decision.limit
            details["remaining"] = decision.remaining
            details["synced_with_server"] = decision.synced
        return details


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _looks_like_quota(body: str) -> bool:
    lowered = body.lower()
    return any(keyword in lowered for keyword in _QUOTA_KEYWORDS)




