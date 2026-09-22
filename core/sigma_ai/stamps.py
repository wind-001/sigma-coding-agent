"""会话时间戳：**给人看的本地时间**，精确到毫秒。

为什么不是 Unix 秒整数
    会话文件（``~/.sigma/sessions/*.jsonl``）是**要被人打开看的**。
    ``1790068491`` 这种数字读不出时刻——想确认"这条消息是什么时候的"，
    得先拿去换算。换成 ``2026-09-22T17:14:20.123`` 就直接可读。

    **这是星辰 2026-09-22 明确要求的**，而且他说旧记录不用管——
    所以这次不做向后兼容：字段类型从 ``int`` 变成 ``str``，
    旧文件里的整数会让 ``message_from_dict`` 报错。
    （口径写在 :func:`from_epoch` 与 ``store.py`` 的注释里。）

**本地时间，刻意不带时区偏移**
    这是本机开发工具，用户要读的就是他自己的钟面时间。
    带 ``+08:00`` 更严谨，但字段会从 23 字符撑到 29，
    而它每条记录出现**两次**。跨时区可比性在本项目里没有需求——
    真要跨时区比对，用 :func:`to_epoch` 转回 Unix 秒再比。
    代价说清楚：**同一个文件在两台不同时区的机器上读出的时刻不同**。

精度取**毫秒而不是微秒**
    同一条消息不会在同一毫秒里出现两次；微秒只会让字段再长 3 位，
    且没有可读性收益。这一条是"够用即止"，不是"越高越好"。
"""

from __future__ import annotations

import time
from datetime import datetime

#: 序列化格式。``T`` 分隔是 ISO-8601；毫秒手工拼三位，
#: 因为 ``%f`` 给的是六位微秒（``.123000``）。
STAMP_PATTERN = "%Y-%m-%dT%H:%M:%S"

#: 紧凑格式（会话 id 用）。没有分隔年份与时分秒的符号，
#: 因为它进的是**文件名**——冒号在 Windows 上是非法字符。
COMPACT_PATTERN = "%Y%m%d-%H%M%S"


def split(epoch: float) -> tuple[datetime, str]:
    """→ ``(2026-09-22 17:14:20 的 datetime, "123")``。

    **毫秒只在这一处算。** 两种格式（可读的与紧凑的）都从这里派生——
    换算散成两份，"毫秒是截断还是四舍五入"这类差异就会在某一处悄悄出现，
    而症状是**同一时刻在两个地方差 1 毫秒**。
    """
    moment = datetime.fromtimestamp(epoch)
    return moment, f"{int(moment.microsecond / 1000):03d}"


def from_epoch(epoch: float) -> str:
    """Unix 秒（可含小数）→ ``2026-09-22T17:14:20.123``（**本地时间**）。

    ``epoch`` 允许是小数：调用方拿到的可能是 ``time.time()``。
    """
    moment, millis = split(epoch)
    return f"{moment.strftime(STAMP_PATTERN)}.{millis}"


def compact(epoch: float) -> str:
    """Unix 秒 → ``20260922-171420.123``（会话 id 的头部）。

    与 :func:`from_epoch` 是**同一个时刻的两种写法**，不是两个概念：
    可读的那个给人看会话文件内容，紧凑的这个进文件名。
    """
    moment, millis = split(epoch)
    return f"{moment.strftime(COMPACT_PATTERN)}.{millis}"


def now() -> str:
    """当前时刻的时间戳。进程里所有"取当前时间"都该走这里。

    **不要散着写 ``from_epoch(time.time())``**：那样"格式是什么"就有多处实现，
    而它一旦要改（比如加时区），漏掉一处就会产出两种形状的字段——
    两者的区别直到有人读文件时才发现。
    """
    return from_epoch(time.time())


def to_epoch(stamp: str) -> float:
    """时间戳 → Unix 秒。给需要比较/排序的地方用（进程内不再存数字）。

    认不出格式就抛 ``ValueError``——**不返回 0、不返回 None**：
    在会话文件里，"读不出的时间戳"意味着文件被改过或是旧格式，
    静默当成 1970 会让"这条消息很旧"这个错误结论传下去。
    """
    return datetime.fromisoformat(stamp).timestamp()
