"""时间戳格式（``sigma_ai.stamps``）的测试。

**这一条来自星辰 2026-09-22 的要求**：会话存储里的时间戳改用
「年月日时分秒毫秒」的具体时间，而不是 Unix 秒整数。

**为什么值得单独一个文件**

    它是**跨层契约**：``sigma_ai``（消息模型）、``sigma_agent``（loop 的时钟）、
    ``sigma_session``（会话文件与 id）、``sigma``（CLI）都用它。
    而"格式"这种东西最容易在某一处被悄悄改掉——
    症状不是报错，是**同一个会话文件里出现两种形状的时间戳**，
    直到有人打开文件才发现。
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from sigma_ai import stamps

#: 年月日 T 时分秒 . 毫秒 —— 逐段钉住，而不是只比一个正则的整体
STAMP_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"\.(?P<millis>\d{3})$"
)


def test_from_epoch_has_all_seven_components() -> None:
    """**需求本体**：年月日时分秒毫秒，一个都不能少。

    逐段断言而不是"能匹配一个宽松正则"：
    后者在"毫秒只有两位"这种真实缺陷面前**仍然会绿**。
    """
    matched = STAMP_RE.match(stamps.from_epoch(1_750_000_000.456))

    assert matched is not None, f"格式不符：{stamps.from_epoch(1_750_000_000.456)}"
    assert matched.group("millis") == "456"


def test_millis_are_not_rounded_away() -> None:
    """毫秒是**三位、零填充**，且不是四舍五入掉的 0。

    两个边界：``.001``（不能变成空或 "1"）与 ``.900``（不能变成 "9"）。
    """
    assert stamps.from_epoch(1_750_000_000.001).endswith(".001")
    assert stamps.from_epoch(1_750_000_000.900).endswith(".900")


def test_it_is_local_time_not_utc() -> None:
    """用的是**本地时间**（``datetime.fromtimestamp`` 而非 ``utcfromtimestamp``）。

    这条不是在"选边"，而是**钉住一个必须显式作出的决定**：
    带不带时区偏移、用本地还是 UTC，都会让字段长得不一样。
    本项目选了本地时间——因为这是本机开发工具，用户要读的是他自己的钟面。
    """
    moment = datetime.fromtimestamp(1_750_000_000)
    assert stamps.from_epoch(1_750_000_000).startswith(moment.strftime("%Y-%m-%dT%H:%M:%S"))


def test_roundtrip_through_to_epoch() -> None:
    """``from_epoch`` → ``to_epoch`` 回到同一时刻（毫秒精度内）。

    不要求**逐位相等**：``to_epoch`` 走的是 datetime 往返，
    而浮点秒在这一步会有极小误差。所以断言的是"差小于 1 毫秒"——
    **把容差写出来**，而不是用一个恰好为真的 ``==`` 蒙过去。
    """
    original = 1_750_000_000.456
    assert abs(stamps.to_epoch(stamps.from_epoch(original)) - original) < 0.001


def test_unparseable_stamp_raises() -> None:
    """认不出的格式 → **抛**，不返回 0 也不返回 None。

    静默当成 1970 会让"这条消息很旧"这个错误结论一路传下去。
    """
    with pytest.raises(ValueError):
        stamps.to_epoch("不是时间戳")


def test_split_and_the_two_formats_agree_on_millis() -> None:
    """可读串与紧凑串**必须是同一时刻**，毫秒尤其不能两样。

    这正是把毫秒计算收进 :func:`stamps.split` 的理由——
    换算散成两份，"截断还是四舍五入"的差异会在某一处悄悄出现。
    """
    epoch = 1_750_000_000.456
    readable = stamps.from_epoch(epoch)
    packed = stamps.compact(epoch)

    assert readable.endswith(packed[-3:]), "两处的毫秒不一致"
    assert STAMP_RE.match(readable) is not None


def test_compact_is_filename_safe() -> None:
    """紧凑格式**不含冒号**——它要进文件名，而冒号在 Windows 上非法。

    这条是"格式选择要有理由"的具体化：好看不是理由，**能当文件名**才是。
    """
    assert ":" not in stamps.compact(1_750_000_000.456)


def test_now_looks_like_a_stamp() -> None:
    """``now()`` 产出的形状与 ``from_epoch`` 一致（两者不能各走各的）。"""
    assert STAMP_RE.match(stamps.now()) is not None
