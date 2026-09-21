"""Firecrawl 额度账本（工具层内部件）。

它保护的是什么
    Firecrawl 免费档 **1,000 credits/月，每月刷新**（2026-09-21 实测：
    `planCredits=1000`，计费周期 2026-09-21 → 2026-10-21）。
    一次 `scrape` = **1 credit**（实测 `remainingCredits` 1000 → 999）。
    超额即禁用本工具，不做自动放宽——宁可这条任务做不成，也不越界花钱
    （与批次 7 对 Tavily 的立场一致）。

两道闸，不是一个
    1. **全局月度**：落盘 `~/.sigma/firecrawl_usage.json`，跨会话、跨工作区。
       它防的是"这个月用超了"。
    2. **进程内会话闸**（`SESSION_FETCH_CAP`，在 `web_fetch.py` 里判）：
       它防的是**另一件事**——一个跑飞的 loop 在 20 轮里把整月额度烧掉。
       只靠月度闸的后果是"额度没了，而且要等下个月"，
       而正确的失败姿态是"这次任务别再抓了，用已有的搜索结果"。

为什么 limit 的初值写死在代码里
    1,000 是 2026-09-21 实测值，不是猜的；而且第一次校准就会被
    `planCredits` 覆盖（`_parse_usage` 就是干这个的）。
    写死它只是为了"还没校准过就先有额度可用"。
"""

from __future__ import annotations

from typing import Any

from sigma_tools._credit_ledger import CreditLedger

#: 官方文档：`https://api.firecrawl.dev/v2/scrape`（v2 是当前版本，v1 仍在但不要用）
FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v2"

#: 实测值（2026-09-21）：免费档 1000 credits，每月刷新
FREE_MONTHLY_CREDITS = 1000

#: 一次 scrape 的单价（实测：调用前后 remainingCredits 差 1）
SCRAPE_COST = 1

#: 进程内第二道闸：一次会话最多抓多少条。
#: 10 的依据：一次"复杂调研"按政策也就抓 1~2 条，10 条足够覆盖多轮追问，
#: 又不至于让一次跑飞的任务把整月额度（1000）花掉 1% 以上。
SESSION_FETCH_CAP = 10

#: httpx 侧的等待上限。**必须比请求体里的 `timeout` 大**，
#: 否则会出现"服务端还在处理、我们这边先超时"——那种情况按纪律不退额度
#: （请求可能已在服务端发生），于是白记一次。让服务端先放弃更干净。
FETCH_TIMEOUT_S = 30.0

#: 请求体里的超时（毫秒）。官方默认 60000，这里收到 25s：宁可拿到一个
#: 明确的失败，也不要挂在一个慢站上把整轮对话拖住。
API_TIMEOUT_MS = 25_000

#: 接受多久以内的缓存页面（毫秒）。48 小时：既省一次真实抓取的时间，
#: 又保证"精读到的内容不会是一周前抓的"——**时效性是本批次的主题**。
MAX_CACHE_AGE_MS = 2 * 24 * 60 * 60 * 1000


class FirecrawlQuota(CreditLedger):
    """Firecrawl 的额度账本。校准端点返回剩余额度而不是已用量。"""

    LABEL = "网页精读"
    TOOL_NAME = "web_fetch"
    USAGE_PATH = "/team/credit-usage"
    DEFAULT_BASE_URL = FIRECRAWL_BASE_URL

    def _parse_usage(self, payload: Any) -> tuple[int, int] | None:
        """Firecrawl 的形状（实测）：

        ``{"success": true, "data": {"remainingCredits": 999, "planCredits": 1000, ...}}``

        注意它给的是**剩余**额度，与 Tavily 的"已用量"相反——
        这个差异正是本方法必须由子类实现、而不能写进基类的原因。
        少了这一步换算，账本会安静地反向漂移（把剩余当已用），
        而症状是"额度用了一点就不能用了"，很难指向根因。
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        remaining = data.get("remainingCredits")
        plan = data.get("planCredits")
        if not isinstance(remaining, int) or not isinstance(plan, int) or plan <= 0:
            return None
        if remaining < 0:
            remaining = 0
        return max(0, plan - remaining), plan
