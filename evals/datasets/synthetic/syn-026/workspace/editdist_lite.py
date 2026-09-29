"""列文斯登编辑距离(插入 / 删除 / 替换,每步代价 1)与最优操作序列回溯。

约定:
- ``distance(a, b)``:把 a 改成 b 最少需要的单字符编辑步数;
- ``operations(a, b)``:回溯出**一条**最优编辑路径,按**执行顺序**返回
  ``Step`` 元组;``Step.kind`` ∈ ``keep / insert / delete / replace``,
  ``char`` 记该步涉及的字符:keep 与 delete 取 a 的字符,
  insert 与 replace 取 b 的字符(替换后的**新**字符);
- 回溯规则(保证结果唯一、可测):从 ``(len(a), len(b))`` 往回走——
  1. 两个字符相等 → ``keep``;
  2. 否则在 replace / delete / insert 三个前驱里取 dp 值最小者,
     **并列时按 replace → delete → insert 的顺序**取先者;
- 空串同样成立(``distance("abc", "")`` 是 3)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    """一步编辑:kind 取 keep / insert / delete / replace。"""

    kind: str
    char: str


#: 并列时的回溯优先级:replace 先于 delete 先于 insert。
_ORDER: dict[str, int] = {"insert": 0, "delete": 1, "replace": 2}


def _dp_table(a: str, b: str) -> list[list[int]]:
    """完整 DP 表;dp[i][j] = a[:i] 与 b[:j] 的编辑距离。"""
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = 0
    for j in range(len(b) + 1):
        dp[0][j] = 0
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            dp[i][j] = 1 + min(dp[i - 1][j - 1], dp[i - 1][j], dp[i][j - 1])
    return dp


def distance(a: str, b: str) -> int:
    """a 改成 b 的最少编辑步数。"""
    return _dp_table(a, b)[len(a)][len(b)]


def operations(a: str, b: str) -> tuple[Step, ...]:
    """回溯一条最优编辑路径,按执行顺序返回。"""
    dp = _dp_table(a, b)
    steps: list[Step] = []
    i, j = len(a), len(b)
    while i > 0 or j > 0:
        if i > 0 and j > 0 and a[i - 1] == b[j - 1]:
            steps.append(Step("keep", a[i - 1]))
            i -= 1
            j -= 1
            continue
        candidates: list[tuple[int, int, str, str]] = []
        if i > 0 and j > 0:
            candidates.append((dp[i - 1][j - 1], _ORDER["replace"], "replace", b[j - 1]))
        if i > 0:
            candidates.append((dp[i - 1][j], _ORDER["delete"], "delete", a[i - 1]))
        if j > 0:
            candidates.append((dp[i][j - 1], _ORDER["insert"], "insert", b[j - 1]))
        _, _, kind, char = min(candidates)
        steps.append(Step(kind, char))
        if kind == "replace":
            i -= 1
            j -= 1
        elif kind == "delete":
            i -= 1
        else:
            j -= 1
    return tuple(steps)
