"""来源质量与时效的**硬规则**（工具层内部件）。

它解决什么问题
    提示词里可以写"丢弃低质量来源、优先近期资料"，但那是**推断型**判断
    （pi 笔记 13.3）：模型可能读漏、可能心软、可能被标题骗。
    本模块把其中**不需要理解**的两类判断做成计算型规则，在结果进入模型视野
    **之前**执行（pi 笔记 13.5 Keep Quality Left）：

    1. **黑名单**：确定的低质来源（内容农场 / 低质 UGC / 广告站）直接不进上下文；
    2. **时间预过滤**：摘要里能识别出、且明确过期的结果直接丢掉。

黑名单只许漏，不许误杀
    `NEVER_BLOCK` 里的权威来源**先判先放行**。理由是代价不对称：
    漏掉一条低质结果，模型顶多多看一眼；误杀一条权威结果（比如把
    StackOverflow 当"论坛"过滤掉），模型会以为"网上没有这个答案"，
    然后开始编——**后者是无法从输出里看出错的错误**。

为什么不做 `forum.*` / `bbs.*` 通配
    官方社区常挂在 `forum.<project>.<tld>`（`forum.djangoproject.com` 就是权威来源）。
    通配会把这些一起杀掉，违反上一条。所以论坛过滤靠**路径标记 + 显式清单**，
    宁可漏一些。这条要在星辰给最终清单时一起确认。

「丢弃必须可见」
    本模块只负责判断，不负责告知。调用方**必须把丢弃条数写进给模型看的
    `content`**（不是 `details`）——模型看不见丢弃，就会以为"网上的资料就这么少"，
    然后拿一条旧资料当现状用。这是 `truncate.py` 的同一条教训。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal
from urllib.parse import urlparse

# ----------------------------------------------------------------------
# 阈值
# ----------------------------------------------------------------------

#: 搜索结果（只有摘要）的年龄上限：超过就丢。
#: 730 天 = 2 年。为什么不是 365：库的一年半前的文章仍常常是当前版本的正确写法，
#: 而 365 会把它们全部误杀；为什么不是更松：再松就等于没有时效概念了。
#: 需要旧资料时模型可以显式传 `include_old=True`（那正是这条阀门的用途）。
MAX_RESULT_AGE_DAYS = 730

#: 精读到的正文的年龄上限：超过就**标注**（不丢——credit 已经花掉了）。
MAX_DOC_AGE_DAYS = 730

DropReason = Literal["blocked", "stale"]

# ----------------------------------------------------------------------
# 黑名单
# ----------------------------------------------------------------------

#: 命中的判定：注册域精确匹配，或子域后缀匹配（`csdn.net` 命中 `blog.csdn.net`）。
#:
#: 来源：批次 8 详规 Q3 的默认表。**星辰说要另给一份清单**——
#: 换清单只改这个 frozenset，其余代码一行不动。
BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        # 低质 UGC / 问答
        "quora.com",
        "answers.com",
        "answers.yahoo.com",
        "zhidao.baidu.com",
        "tieba.baidu.com",
        "wenku.baidu.com",
        "ask.csdn.net",
        # 内容农场 / AI 生成 SEO
        #   csdn.net 进：pi 笔记 0.5 / 1 节的旁证是"版本、包名、star 数在中文
        #   二手资料里几乎全是错的"，而它是这类内容的最大集中地。
        #   runoob.com / cnblogs.com 不进：前者仍是很多人的唯一中文入门源，
        #   后者有大量高质量原创——靠时间预过滤削峰，不靠黑名单一刀切。
        "csdn.net",
        "jianshu.com",
        "51cto.com",
        "linuxidc.com",
        "php.cn",
        "itdaan.com",
        "pianshen.com",
        "codeleading.com",
        "geeksforgeeks.org",
        "javatpoint.com",
        "tutorialspoint.com",
        "w3schools.com",
        "programiz.com",
        # 下载 / 广告站（download.csdn.net 已被 csdn.net 覆盖，留着是为了自明）
        "download.csdn.net",
        "crx4chrome.com",
    }
)

#: **先判先放行**。列在这里的域，即使命中上面的表或路径标记也不拦。
#: 判据是"这个来源出错时，我们有没有别的来源可替代"——答案是没有的来源不该被静态规则杀掉。
NEVER_BLOCK: frozenset[str] = frozenset(
    {
        "github.com",
        "stackoverflow.com",
        "stackexchange.com",
        "python.org",
        "developer.mozilla.org",
        "pypi.org",
        "readthedocs.io",
        "readthedocs.org",
        "npmjs.com",
        "nodejs.org",
        "kernel.org",
        "rust-lang.org",
        "go.dev",
        "docs.rs",
    }
)

#: 路径标记：论坛/BBS 的典型路径。只匹配路径，不看域名——见模块 docstring 的说明。
BLOCKED_PATH_MARKERS: tuple[str, ...] = (
    "/forum/",
    "/forums/",
    "/bbs/",
    "/thread/",
    "/showthread.php",
    "/viewtopic.php",
)


def _host_of(url: str) -> str:
    """取规范化主机名。认不出返回空串（**不猜**，猜错就是误杀）。"""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def is_blocked_url(url: str) -> bool:
    """这条 URL 是否属于静态黑名单。"""
    host = _host_of(url)
    if not host:
        return False
    if _matches(host, NEVER_BLOCK):
        return False
    if _matches(host, BLOCKED_DOMAINS):
        return True
    path = urlparse(url).path.lower()
    return any(marker in path for marker in BLOCKED_PATH_MARKERS)

# ----------------------------------------------------------------------
# 时间识别
# ----------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# 2026-09-21 / 2026/9/21 / 2026.9.21
_ISO_DATE = re.compile(r"(?<![\d.])(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?![\d.])")

# 2026年9月21日 / 2026 年 9 月（只有年月时按 1 号算，方向保守：旧资料更容易被丢）
_CN_DATE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")

# Sep 21, 2026 / September 21 2026
_EN_DATE = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})(?!\d)"
)

# 21 Sep 2026
_EN_DATE_REV = re.compile(
    r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})(?!\d)"
)

# 3 days ago / 2 weeks ago
_REL_AGO = re.compile(r"(?<!\d)(\d{1,3})\s*(day|week|month|year)s?\s+ago", re.IGNORECASE)

# 3天前 / 2周前 / 5个月前 / 1年前（"个月"要排在"月"前面，正则引擎按分支顺序匹配）
_REL_CN = re.compile(r"(\d{1,3})\s*(个月|天|周|月|年)前")

# last week / past month
_REL_LAST = re.compile(r"\b(?:last|past)\s+(day|week|month|year)\b", re.IGNORECASE)

# 注意：这里**不能用 `\b`**。Python 的 `\w` 把中日韩汉字也算作"词字符"，
# 所以"昨天更新"里"天"与"更"之间**没有**词边界，`\b...\b` 会匹配失败。
# 改用"前后不得紧邻 ASCII 字母/数字"的环视——对英文（yesterday）与中文（昨天）都成立。
_YESTERDAY = re.compile(r"(?<![A-Za-z0-9])(yesterday|昨天)(?![A-Za-z0-9])", re.IGNORECASE)

_AGO_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30, "year": 365}

_CN_AGO_DAYS: dict[str, int] = {"天": 1, "周": 7, "月": 30, "个月": 30, "年": 365}


def _safe_date(year: int, month: int, day: int) -> date | None:
    """构造日期，非法值返回 None。

    **宁可认不出，也不要猜**：`2026-13-45` 这种来自版本号或残句的数字，
    猜出来的结果是一个假日期，而假日期会安静地决定一条结果丢不丢。
    """
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _absolute_candidates(text: str) -> list[date]:
    found: list[date] = []
    for match in _ISO_DATE.finditer(text):
        candidate = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if candidate is not None:
            found.append(candidate)

    for match in _CN_DATE.finditer(text):
        day = int(match.group(3)) if match.group(3) else 1
        candidate = _safe_date(int(match.group(1)), int(match.group(2)), day)
        if candidate is not None:
            found.append(candidate)

    for match in _EN_DATE.finditer(text):
        month = _MONTHS.get(match.group(1).lower()[:3])
        if month is None:
            continue
        candidate = _safe_date(int(match.group(3)), month, int(match.group(2)))
        if candidate is not None:
            found.append(candidate)

    for match in _EN_DATE_REV.finditer(text):
        month = _MONTHS.get(match.group(2).lower()[:3])
        if month is None:
            continue
        candidate = _safe_date(int(match.group(3)), month, int(match.group(1)))
        if candidate is not None:
            found.append(candidate)

    return found


def _relative_candidates(text: str, now: date) -> list[date]:
    found: list[date] = []
    for match in _REL_AGO.finditer(text):
        days = int(match.group(1)) * _AGO_DAYS[match.group(2).lower()]
        found.append(now - timedelta(days=days))

    for match in _REL_CN.finditer(text):
        days = int(match.group(1)) * _CN_AGO_DAYS[match.group(2)]
        found.append(now - timedelta(days=days))

    for match in _REL_LAST.finditer(text):
        days = _AGO_DAYS[match.group(1).lower()]
        found.append(now - timedelta(days=days))

    if _YESTERDAY.search(text):
        found.append(now - timedelta(days=1))

    return found


def detect_published_at(text: str, *, now: date) -> date | None:
    """从标题/摘要/正文里抽出**最新的**可识别日期；认不出返回 None。

    为什么取最新而不是第一个：
        摘要里常常同时提到旧日期（"2023 年引入的 API"）和页面自己的日期。
        取第一个会把"新文章里提到的旧日期"当成发布时间，于是**误杀**。
        取最新的方向是保守的（它只会让"看起来更新"的那条活下来）。

    认不出为什么不能当成"新"：
        那是把"无法判断"和"确认很新"混为一谈。这里返回 None，
        由调用方决定（搜索侧：保留；精读侧：标注"发布时间未识别"）。
    """
    if not text:
        return None
    # 未来日期不是发布时间（时区/占位符/解析误差），一律忽略
    horizon = now + timedelta(days=1)
    found = [
        candidate
        for candidate in (*_absolute_candidates(text), *_relative_candidates(text, now))
        if candidate <= horizon
    ]
    if not found:
        return None
    return max(found)


def age_days(published: date, now: date) -> int:
    """已发布多少天。"""
    return (now - published).days


def describe_age(days: int) -> str:
    """把天数说成人话（进给模型看的提示语）。"""
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 30:
        return f"约 {days} 天前"
    if days < 365:
        return f"约 {days // 30} 个月前"
    years = days // 365
    return f"约 {years} 年{'多' if days % 365 > 180 else ''}前"


def is_stale(published: date, *, now: date, max_age_days: int = MAX_DOC_AGE_DAYS) -> bool:
    """发布时间是否已超出时效范围。"""
    return age_days(published, now) > max_age_days


def drop_reason(
    url: str, text: str, *, now: date, include_old: bool
) -> DropReason | None:
    """这条结果该不该在进入模型视野之前被丢掉。None = 保留。

    两条规则的顺序是有意的：**黑名单先判**。
    一条被黑名单命中的结果，不值得再花时间解析它的日期
    （解析结果是"它有多旧"也改变不了"它不该被信任"）。
    """
    if is_blocked_url(url):
        return "blocked"
    if include_old:
        return None
    published = detect_published_at(text, now=now)
    if published is None:
        return None
    return "stale" if is_stale(published, now=now, max_age_days=MAX_RESULT_AGE_DAYS) else None

