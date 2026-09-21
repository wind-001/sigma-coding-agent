"""来源硬规则（黑名单 / 时间预过滤）的测试。

全部是纯函数：没有网络、没有 key、没有时钟依赖（`now` 是显式参数）——
**"今天"必须注入**，否则"2019 年的结果会被丢掉"这类断言会随真实日期漂移，
症状是"今天绿明天红"，而排查方向会全错（去查过滤逻辑，而不是查日期）。

这一层值得单独测，因为它是**模型看不到的判断**：
一旦黑名单误杀或日期解析出错，症状是"搜索结果莫名变少"，
在集成层几乎无法归因。
"""

from __future__ import annotations

from datetime import date, timedelta

from sigma_tools._source_policy import (
    MAX_RESULT_AGE_DAYS,
    age_days,
    describe_age,
    detect_published_at,
    drop_reason,
    is_blocked_url,
    is_stale,
)

NOW = date(2026, 9, 21)


# ----------------------------------------------------------------------
# 时间识别
# ----------------------------------------------------------------------


def test_absolute_date_formats() -> None:
    """四种绝对格式都要认出来（中英混合的搜索结果里这四种最常见）。"""
    assert detect_published_at("posted 2026-09-05", now=NOW) == date(2026, 9, 5)
    assert detect_published_at("2026/9/5", now=NOW) == date(2026, 9, 5)
    assert detect_published_at("2026.9.5", now=NOW) == date(2026, 9, 5)
    assert detect_published_at("Sep 5, 2026", now=NOW) == date(2026, 9, 5)
    assert detect_published_at("September 5, 2026", now=NOW) == date(2026, 9, 5)
    assert detect_published_at("5 Sep 2026", now=NOW) == date(2026, 9, 5)
    assert detect_published_at("2026年9月5日", now=NOW) == date(2026, 9, 5)


def test_chinese_year_month_without_day_uses_first_day() -> None:
    """只有年月时按 1 号算——方向是保守的（旧资料更容易被判超期）。"""
    assert detect_published_at("2024年3月更新", now=NOW) == date(2024, 3, 1)


def test_relative_dates() -> None:
    assert detect_published_at("3 days ago", now=NOW) == date(2026, 9, 18)
    assert detect_published_at("2 weeks ago", now=NOW) == date(2026, 9, 7)
    assert detect_published_at("last month", now=NOW) == date(2026, 8, 22)
    assert detect_published_at("1年前", now=NOW) == date(2025, 9, 21)
    assert detect_published_at("5个月前更新", now=NOW) == date(2026, 4, 24)
    assert detect_published_at("昨天更新", now=NOW) == date(2026, 9, 20)


def test_chinese_relative_word_needs_no_ascii_word_boundary() -> None:
    """中文相对时间**不能**用 `\\b` 定界。

    Python 的 `\\w` 把汉字算作词字符，所以"昨天更新"里"天"与"更"之间没有词边界，
    `\\b昨天\\b` 会**静默不匹配**——结果是中文页面永远识别不出发布时间，
    而所有中文结果因此逃过时间预过滤。这是一个只影响中文的静默失效。
    """
    assert detect_published_at("昨天更新", now=NOW) == date(2026, 9, 20)
    assert detect_published_at("本文昨天发布", now=NOW) == date(2026, 9, 20)
    # 英文侧仍要定界：不要让 yes-terday 这种碎片误命中
    assert detect_published_at("notyesterdayx", now=NOW) is None


def test_unrecognizable_text_returns_none() -> None:
    """认不出就返回 None——**不猜**。

    "2026" 这种裸年份必须不算日期：它是版本号、版权年和 star 数的高发形状，
    认成日期就会把一条正常结果误杀。
    """
    assert detect_published_at("", now=NOW) is None
    assert detect_published_at("no dates here at all", now=NOW) is None
    assert detect_published_at("Copyright 2026 Example Inc.", now=NOW) is None
    assert detect_published_at("Python 3.13 ships a new REPL", now=NOW) is None


def test_invalid_and_future_dates_are_ignored() -> None:
    """非法日期与未来日期都不算。假日期会安静地决定一条结果丢不丢。"""
    assert detect_published_at("2026-13-45", now=NOW) is None
    assert detect_published_at("2099-01-01", now=NOW) is None


def test_latest_date_wins() -> None:
    """正文里同时出现旧日期与页面日期时，取**最新**那个。

    取第一个会把"新文章里提到的旧日期"当成发布时间，于是误杀新资料。
    """
    text = "本文基于 2019-01-01 的接口，最后更新：2026-05-05"
    assert detect_published_at(text, now=NOW) == date(2026, 5, 5)


def test_age_helpers() -> None:
    assert age_days(date(2026, 9, 21), NOW) == 0
    assert describe_age(0) == "今天"
    assert describe_age(1) == "昨天"
    assert describe_age(3) == "约 3 天前"
    assert describe_age(90) == "约 3 个月前"
    assert describe_age(400) == "约 1 年前"
    assert is_stale(date(2019, 1, 1), now=NOW) is True
    assert is_stale(date(2026, 5, 1), now=NOW) is False


def test_describe_age_multi_year_threshold() -> None:
    """整年之外的余数要**超过半年**才叫"多"。

    800 天 = 2 年 + 70 天，余数只有两个月，说"2 年多"是把"多"的门槛降到 2 个月——
    那样 400 天（1 年 + 35 天）就该说"1 年多"，与上一行"约 1 年前"自相矛盾。
    判据是同一个数字，不能两处口径不同。
    """
    assert describe_age(730) == "约 2 年前"      # 恰好 2 年
    assert describe_age(800) == "约 2 年前"      # 2 年 + 70 天，余数不足半年
    assert describe_age(912) == "约 2 年多前"    # 2 年 + 182 天，刚过半年
    assert describe_age(1095) == "约 3 年前"


def test_stale_boundary_is_exclusive() -> None:
    """恰好 730 天**不算**超期（判据是 `>`）。

    边界值写错是这类规则最常见的一种错，而且只有边界上能看出来。
    """
    exactly = NOW - timedelta(days=MAX_RESULT_AGE_DAYS)
    older = NOW - timedelta(days=MAX_RESULT_AGE_DAYS + 1)
    assert is_stale(exactly, now=NOW) is False
    assert is_stale(older, now=NOW) is True



# ----------------------------------------------------------------------
# 黑名单
# ----------------------------------------------------------------------


def test_blocked_domains_and_subdomains() -> None:
    assert is_blocked_url("https://csdn.net/article/1") is True
    assert is_blocked_url("https://blog.csdn.net/user/article/1") is True
    assert is_blocked_url("https://www.geeksforgeeks.org/python-lists/") is True
    assert is_blocked_url("https://docs.tavily.com/api-credits") is False
    assert is_blocked_url("https://blog.python.org/2026/05/thing.html") is False


def test_path_markers_block_forum_urls() -> None:
    """论坛靠**路径标记**判：不做 `forum.*` 通配（会误伤官方社区）。"""
    assert is_blocked_url("https://example.com/forum/topic/1") is True
    assert is_blocked_url("https://example.com/bbs/showthread.php?t=1") is True
    assert is_blocked_url("https://example.com/thread/abc") is True
    assert is_blocked_url("https://example.com/blog/post-1") is False


def test_never_block_wins_over_blacklist() -> None:
    """**误杀是不可恢复的错误**：权威来源即使命中路径标记也要放行。

    这条用例是整张黑名单的安全阀——把 StackOverflow 当"论坛"过滤掉的后果，
    是模型以为"网上没有答案"，然后开始编。
    """
    assert is_blocked_url("https://stackoverflow.com/questions/1/forum/discuss") is False
    assert is_blocked_url("https://github.com/psf/requests/issues/1/thread/x") is False
    assert is_blocked_url("https://docs.python.org/3/forum/") is False


def test_unparsable_url_is_not_blocked() -> None:
    """认不出主机名就不拦：无法判断 ≠ 低质量（同"无法判断时效 ≠ 老旧"）。"""
    assert is_blocked_url("") is False
    assert is_blocked_url("not-a-url") is False


# ----------------------------------------------------------------------
# 合起来：drop_reason
# ----------------------------------------------------------------------


def test_drop_reason_priority_and_include_old() -> None:
    """黑名单先于时间判；`include_old` 只放开时间、不放开黑名单。"""
    old_text = "最后更新：2019-05-05"
    fresh_text = "最后更新：2026-05-05"

    assert drop_reason("https://ok.example/a", fresh_text, now=NOW, include_old=False) is None
    assert drop_reason("https://ok.example/a", old_text, now=NOW, include_old=False) == "stale"
    assert drop_reason("https://ok.example/a", old_text, now=NOW, include_old=True) is None
    assert (
        drop_reason("https://blog.csdn.net/a", fresh_text, now=NOW, include_old=True)
        == "blocked"
    )
    # 认不出时间 → 保留（宁可放过，不可错杀）
    assert (
        drop_reason("https://ok.example/a", "无日期的一篇文章", now=NOW, include_old=False)
        is None
    )
