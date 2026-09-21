"""批次 8 门槛注入实验：逐条证伪 G42–G47。

沿用批次 2–4 / 7 的框架（在真实仓库上改、跑、finally 还原），**不另写一份 Repo**——
验证工具自身会腐化，两份实现意味着修一个忘另一个。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E48 | G42 单次抓取上限 2 条 | 去掉 `[: self._max_urls]` 截断 | 5 条 URL 发出 5 次请求（多花 3 credits）|
| E49 | G43 黑名单在代码层 | `is_blocked_url` 恒 False | 黑名单 URL 发出请求 / 出现在搜索输出里 |
| E50 | G44 时间预过滤 | `drop_reason` 恒保留 | 2019 年的结果混进上下文、计数变 0 |
| E51 | G45 丢弃必须可见 | 不再拼 `_filter_summary` | content 里没有过滤计数行 |
| E52 | G46 Firecrawl 额度硬闸 | 忽略 `decision.allowed` | 额度用尽仍发出 /scrape 请求 |
| E53 | G47 错误不穿透 | except 里直接 raise | 失败变成异常而不是模型可见的结果 |

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch8.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

SOURCE_POLICY = "core/sigma_tools/_source_policy.py"
WEB_SEARCH = "core/sigma_tools/web_search.py"
WEB_FETCH = "core/sigma_tools/web_fetch.py"

WS_TESTS = "tests/test_tools_web_search.py"
WF_TESTS = "tests/test_tools_web_fetch.py"


def _inject_e48(repo: Repo) -> None:
    """E48 / G42：去掉单次上限。

    症状是"模型一次点 5 条，5 条全抓"——3 条白花的 credit，
    而模型与用户都看不到任何异常（上限本来就不在它的视野里）。
    """
    repo.patch(
        WEB_FETCH,
        "        wanted, dropped = params.urls[: self._max_urls], params.urls[self._max_urls:]",
        "        wanted, dropped = params.urls, []  # 注入：上限形同虚设",
    )


def _inject_e49(repo: Repo) -> None:
    """E49 / G43：黑名单形同虚设。

    内容农场的结果会重新出现在上下文里，并且 web_fetch 会为它花 credit。
    """
    repo.patch(
        SOURCE_POLICY,
        "def is_blocked_url(url: str) -> bool:\n"
        "    \"\"\"这条 URL 是否属于静态黑名单。\"\"\"\n"
        "    host = _host_of(url)",
        "def is_blocked_url(url: str) -> bool:\n"
        "    return False  # 注入：黑名单形同虚设\n"
        "    host = _host_of(url)",
    )


def _inject_e50(repo: Repo) -> None:
    """E50 / G44：时间预过滤形同虚设。

    症状是"2019 年的写法与上个月的写法混在一起"，而模型没有任何时效线索。
    """
    repo.patch(
        SOURCE_POLICY,
        "    if include_old:\n        return None\n    published = detect_published_at(text, now=now)",
        "    if True:  # 注入：时间预过滤形同虚设\n        return None\n    published = detect_published_at(text, now=now)",
    )


def _inject_e51(repo: Repo) -> None:
    """E51 / G45：丢弃不可见。

    过滤照常生效，但模型不知道——它会以为"网上的资料就这么少"。
    """
    repo.patch(
        WEB_SEARCH,
        "    summary = _filter_summary(filtered)\n    if summary:\n        lines.append(summary)",
        "    if False:  # 注入：丢弃不可见\n        lines.append(_filter_summary(filtered))",
    )


def _inject_e52(repo: Repo) -> None:
    """E52 / G46：忽略额度判定。

    这是"先花钱再发现没钱"的形态——闸门形同不存在。
    """
    repo.patch(
        WEB_FETCH,
        "            decision = await self._quota.reserve(SCRAPE_COST)\n            if not decision.allowed:",
        "            decision = await self._quota.reserve(SCRAPE_COST)\n"
        "            if False:  # 注入：忽略额度判定",
    )


def _inject_e53(repo: Repo) -> None:
    """E53 / G47：让错误穿透。

    工具抛异常等于把纠错能力关掉（架构 4.3 节第 3 点）。
    """
    repo.patch(
        WEB_FETCH,
        "            except FetchFailure as exc:\n                if exc.quota_exhausted:",
        "            except FetchFailure as exc:\n"
        "                raise  # 注入：错误穿透\n                if exc.quota_exhausted:",
    )


def main() -> int:
    experiment("G42", "单次抓取上限 2 条（截断在扣额度之前）", WF_TESTS, _inject_e48)
    experiment("G43", "黑名单在代码层（web_fetch 纵深防御）", WF_TESTS, _inject_e49)
    experiment("G43b", "黑名单在代码层（web_search 不进上下文）", WS_TESTS, _inject_e49)
    experiment("G44", "时间预过滤（超期结果被丢弃）", WS_TESTS, _inject_e50)
    experiment("G45", "丢弃必须可见（content 里有计数）", WS_TESTS, _inject_e51)
    experiment("G46", "Firecrawl 额度硬闸（用尽即零请求）", WF_TESTS, _inject_e52)
    experiment("G47", "错误不穿透（失败转结果不抛异常）", WF_TESTS, _inject_e53)

    print()
    print("=" * 78)
    ok = True
    for gate, what, passed, detail in RESULTS:
        flag = "PASS" if passed else "FAIL"
        ok = ok and passed
        print(f"[{flag}] {gate}  {what}")
        if not passed:
            print(f"        {detail}")
    proven = sum(1 for row in RESULTS if row[2])
    print("=" * 78)
    print(f"共 {len(RESULTS)} 条注入实验，{proven} 条被成功证伪")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
