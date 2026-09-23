"""把一笔总额按权重分摊成若干份（单位：分）。

账单分摊、配额下发这类场景都用它。两条硬要求：

1. **分摊完各份之和必须正好等于总额**——不能因为取整把账弄丢；
2. 每一份与"理想份额"的偏差不超过 1 分。

余数的归属采用**最大余数法**（与议席分配同规则）：余数相同的时候，
靠前的权重先得。
"""

from __future__ import annotations


def allocate(total: int, weights: list[int]) -> list[int]:
    """把 ``total``（整数分）按 ``weights`` 的比例分摊。

    空权重表、负总额、负权重、权重和为 0 都是调用方的 bug，
    直接抛 ``ValueError``，不猜。
    """
    if not weights:
        raise ValueError("weights 不能为空")
    if total < 0:
        raise ValueError(f"total 不能为负数，收到 {total}")
    if any(w < 0 for w in weights):
        raise ValueError("权重不能为负数")
    if sum(weights) == 0:
        raise ValueError("权重之和不能为 0")

    return [int(total * w / sum(weights)) for w in weights]
