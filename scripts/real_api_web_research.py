"""真实 API 冒烟：web_search（Tavily）+ web_fetch（Firecrawl）各一次。

**手动运行、不进 CI、消耗 2 credits**（Tavily 1 + Firecrawl 1）。
用途：确认两端协议形状没有变（批次 7 / 8 的教训：协议形状以实测为准）。

用法（密钥从 .env / 环境变量解析，本脚本不自己猜路径）：
    ./.venv/Scripts/python.exe scripts/real_api_web_research.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sigma_agent.types import ToolContext
from sigma_ai.base import NeverCancelled
from sigma.dotenv import USER_CONFIG_DIR, resolve_firecrawl_api_key, resolve_tavily_api_key
from sigma_tools._firecrawl_quota import FirecrawlQuota
from sigma_tools._tavily_quota import TavilyQuota
from sigma_tools.web_fetch import WebFetchTool
from sigma_tools.web_search import WebSearchTool


async def main() -> None:
    tavily_key, tavily_source = resolve_tavily_api_key()
    firecrawl_key, firecrawl_source = resolve_firecrawl_api_key()
    if not tavily_key or not firecrawl_key:
        raise SystemExit(
            f"缺密钥：TAVILY={bool(tavily_key)}（{tavily_source}），"
            f"FIRECRAWL={bool(firecrawl_key)}（{firecrawl_source}）"
        )

    state_dir = USER_CONFIG_DIR
    state_dir.mkdir(parents=True, exist_ok=True)

    search = WebSearchTool(
        api_key=tavily_key,
        quota=TavilyQuota(api_key=tavily_key, state_path=state_dir / "tavily_usage.json"),
    )
    fetch = WebFetchTool(
        api_key=firecrawl_key,
        quota=FirecrawlQuota(
            api_key=firecrawl_key, state_path=state_dir / "firecrawl_usage.json"
        ),
    )

    real_ctx = ToolContext(
        session_id="smoke", workspace_root=Path.cwd(), signal=NeverCancelled()
    )

    search_args = search.params.model_validate({"query": "firecrawl api credits pricing"})
    search_result = await search.run(search_args, real_ctx)
    print("=== web_search ===")
    print(f"is_error={search_result.is_error} details={search_result.details}")
    print(search_result.content[0].text[:600])

    first_url = next(
        (
            ref["url"]
            for ref in search_result.details.get("results", [])
            if isinstance(ref.get("url"), str) and ref["url"].startswith("http")
        ),
        "https://docs.firecrawl.dev/introduction",
    )
    fetch_args = fetch.params.model_validate({"urls": [first_url], "reason": "冒烟"})
    fetch_result = await fetch.run(fetch_args, real_ctx)
    print("=== web_fetch ===")
    print(f"is_error={fetch_result.is_error} details={fetch_result.details}")
    print(fetch_result.content[0].text[:600])


if __name__ == "__main__":
    asyncio.run(main())
