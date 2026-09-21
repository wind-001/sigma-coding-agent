"""Tavily 免费额度账本（工具层内部件）。

批次 8 起，这里只是 :class:`CreditLedger` 的一个**子类**
    批次 7 时 Tavily 是唯一一家，所以账本逻辑整个写在这个文件里。
    批次 8 加了 Firecrawl，两家的公共部分（落盘 / 锁 / TTL 校准 / 预留-结算 / 耗尽禁用）
    上移到了 `_credit_ledger.py`；这里只剩**属于 Tavily 的两样东西**：

        校准端点（`/usage`）与它的响应解析

    这么切的原因是：**新增第三家只需要新增一个子类**，
    不会再有人去复制一段 150 行的记账逻辑（复制出来的两份会各自漂移，
    而症状是"只有其中一个工具的额度算错了"，不指向根因）。

口径：credits 不是"次数"
    免费档是 1,000 credits/月。basic / fast / ultra-fast 每次 1 credit，
    advanced 每次 2 credits。所以"1000 次/月"只在默认档成立。
    按 credits 记账是唯一不会超额的记法。
"""

from __future__ import annotations

from typing import Any

from sigma_tools._credit_ledger import (
    DEFAULT_TIMEOUT_S,
    CreditLedger,
    QuotaDecision,
)

# `DEFAULT_TIMEOUT_S` / `QuotaDecision` 是**从基类转出来的**：
# 定义搬了家，但 import 路径不变——避免为一次重构去改一堆调用点。
__all__ = [
    "ADVANCED_COST",
    "DEFAULT_COST",
    "DEFAULT_TIMEOUT_S",
    "FREE_MONTHLY_CREDITS",
    "TAVILY_BASE_URL",
    "QuotaDecision",
    "TavilyQuota",
]

TAVILY_BASE_URL = "https://api.tavily.com"

FREE_MONTHLY_CREDITS = 1000

#: advanced 的单价是其余档位的两倍（Tavily 官方计费表）
ADVANCED_COST = 2
DEFAULT_COST = 1


class TavilyQuota(CreditLedger):
    """Tavily 的额度账本。校准端点返回 ``{"key": {"usage", "limit"}}``。"""

    LABEL = "联网搜索"
    TOOL_NAME = "web_search"
    USAGE_PATH = "/usage"
    DEFAULT_BASE_URL = TAVILY_BASE_URL

    def _parse_usage(self, payload: Any) -> tuple[int, int] | None:
        """Tavily 的形状（批次 7 真实调用确认）：``{"key": {"usage": 999, "limit": 1000}}``。"""
        key = payload.get("key") if isinstance(payload, dict) else None
        if not isinstance(key, dict):
            return None
        used = key.get("usage")
        limit = key.get("limit")
        if not isinstance(used, int) or used < 0:
            return None
        if not isinstance(limit, int) or limit <= 0:
            return None
        return used, limit
