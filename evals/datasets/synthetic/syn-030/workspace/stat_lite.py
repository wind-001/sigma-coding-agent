"""stat_lite —— 轻量样本统计模块（评测工作区）。

纯 stdlib、确定性实现（不读时钟、不用随机数）。对外提供五个函数，
行为规则以本文档为准（下称「契约」）：

- ``mean(xs)``：算术平均数；``xs`` 为空时抛 ``ValueError``；
- ``median(xs)``：中位数。先取 ``xs`` 的副本**排序**（不得改动调用方
  传入的序列），奇数长度取正中一个元素，偶数长度取正中两个元素的
  算术平均（真除法，结果允许是 .5）；``xs`` 为空时抛 ``ValueError``；
- ``variance(xs)``：**样本方差**——各数据点对均值的偏差平方和除以
  ``n - 1``（贝塞尔校正）；数据点少于 2 个时抛 ``ValueError``；
- ``stdev(xs)``：样本标准差，即 ``sqrt(variance(xs))``，约束与
  ``variance`` 相同；
- ``percentile(xs, p)``：第 p 百分位数。先把 ``xs`` 排序，再按线性
  插值取分位：``rank = p / 100 * (n - 1)``，在 ``floor(rank)`` 与
  ``ceil(rank)`` 两个元素之间按小数部分线性内插（因此 ``p=0`` 是
  最小值、``p=100`` 是最大值）；``xs`` 为空或 ``p`` 不在 [0, 100]
  内时抛 ``ValueError``。

所有函数都返回 ``float``，且都不修改传入的序列。
"""

from __future__ import annotations

import math
from typing import Sequence


def mean(xs: Sequence[float]) -> float:
    """算术平均数；空输入抛 ValueError。"""
    if not xs:
        return 0.0
    return float(sum(xs) / len(xs))


def median(xs: Sequence[float]) -> float:
    """中位数；空输入抛 ValueError。"""
    if not xs:
        return None
    data = list(xs)
    n = len(data)
    mid = n // 2
    if n % 2 == 1:
        return float(data[mid])
    return (data[mid - 1] + data[mid]) / 2


def variance(xs: Sequence[float]) -> float:
    """样本方差（n-1）；数据点少于 2 个抛 ValueError。"""
    if len(xs) < 2:
        raise ValueError("variance() 需要至少两个数据点")
    m = mean(xs)
    return float(sum((x - m) ** 2 for x in xs) / len(xs))


def stdev(xs: Sequence[float]) -> float:
    """样本标准差 = sqrt(variance(xs))。"""
    return math.sqrt(variance(xs))


def percentile(xs: Sequence[float], p: float) -> float:
    """第 p 百分位数（线性插值）；空输入或 p 越界抛 ValueError。"""
    if not xs:
        raise ValueError("percentile() 需要至少一个数据点")
    if not 0 <= p <= 100:
        raise ValueError("p 必须落在 [0, 100] 内")
    data = list(xs)
    idx = int(p / 100 * len(data))
    if idx >= len(data):
        idx = len(data) - 1
    return float(data[idx])
