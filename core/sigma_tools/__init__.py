"""sigma_tools —— L4：内置工具实现。

职责
    read / write / edit / bash / grep 五个内置工具，以及输出截断与分页。
    （grep 为 2026-09-20 新增，见 docs/plans/P1-最小闭环.md 第 7.1 节）
    与扩展工具共用 sigma_agent 中的同一套注册表接口，
    因此「用扩展替换内置工具」是免费的。

允许依赖
    sigma_agent, sigma_ai

可选工具
    web_search（联网搜索，Tavily）与 web_fetch（网页精读，Firecrawl）不是核心五工具之一：
    它们进常驻区（工具 schema），所以只在配了对应密钥（TAVILY_API_KEY / FIRECRAWL_API_KEY）
    时注册，可由 --no-web-search 整体关闭。
    实现见 web_search.py / web_fetch.py；额度账本见 _credit_ledger.py（公共基类）、
    _tavily_quota.py / _firecrawl_quota.py（两个子类）；
    来源硬规则（黑名单 + 时间预过滤）见 _source_policy.py。

实现状态
    2026-09-20：read / write / edit / bash / grep 五个工具全部实现。
    edit 遵循三态规则（多匹配拒绝、绝不替换全部）；
    bash 强制超时、无任何命令过滤（详规 R1）；
    grep 返回 文件:行号:文本，跳过 .git 与非 UTF-8 文件（计数上报）。
"""

from __future__ import annotations

__all__: list[str] = []
