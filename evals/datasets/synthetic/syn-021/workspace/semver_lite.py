"""语义化版本（semver 2.0.0 核心规则）的比较与范围匹配。

约定（见 semver.org §11 优先级规则）：
- ``parse_version("1.2.3")`` / ``parse_version("1.2.3-alpha.1")`` → ``Version``；
  版本号必须是 ``major.minor.patch``（否则 ValueError），prerelease 可选；
- 比较优先级：major → minor → patch；patch 相同时**正式版高于带 prerelease
  的版本**（``1.2.3`` > ``1.2.3-alpha.1``）；
- prerelease 标识符逐个比较：
  - 两边都是纯数字 → 按**数值**比（``alpha.9 < alpha.10``）；
  - 纯数字标识符**低于**含字母的标识符（``1 < alpha``）；
  - 两边都含字母 → 按 ASCII 比较（``alpha < beta``）；
  - 前面全部相等时，标识符少的一方**低**（``alpha < alpha.1``）；
- ``compare(a, b)``：a<b 返回 -1，相等返回 0，a>b 返回 1；参数可以是
  ``Version`` 或版本字符串；
- ``satisfies(version, range_str)``：
  - 比较子支持 ``>=`` ``<=`` ``>`` ``<`` ``=`` 与裸版本（等价于 ``=``）；
  - **空格分隔的多个比较子是 AND**（``">=1.0.0 <2.0.0"`` 表示闭开区间）；
  - ``||`` 分隔的多个范围是 **OR**，任一范围满足即满足；
  - 范围里的版本同样走完整 semver 比较（含 prerelease 规则）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()


def parse_version(text: str) -> Version:
    """把 ``major.minor.patch[-prerelease]`` 解析成 Version。"""
    core, _, pre = text.partition("-")
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError(f"版本号必须是 major.minor.patch：{text!r}")
    major, minor, patch = (int(p) for p in parts)
    prerelease: tuple[str, ...] = ()
    if pre:
        prerelease = tuple(pre.split("."))
    return Version(major=major, minor=minor, patch=patch, prerelease=prerelease)


def _compare_prerelease(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """按 semver §11 比较两个 prerelease 标识符序列。"""
    for x, y in zip(a, b):
        if x == y:
            continue
        x_num, y_num = x.isdigit(), y.isdigit()
        if x_num and y_num:
            return -1 if x < y else 1
        if x_num != y_num:
            return 1 if x_num else -1
        return -1 if x < y else 1
    if len(a) == len(b):
        return 0
    return -1 if len(a) < len(b) else 1


def compare(a: str | Version, b: str | Version) -> int:
    """完整 semver 比较，返回 -1 / 0 / 1。"""
    va = a if isinstance(a, Version) else parse_version(a)
    vb = b if isinstance(b, Version) else parse_version(b)
    if va.major != vb.major:
        return -1 if va.major < vb.major else 1
    if va.minor != vb.minor:
        return -1 if va.minor < vb.minor else 1
    if va.patch != vb.patch:
        return -1 if va.patch < vb.patch else 1
    return _compare_prerelease(va.prerelease, vb.prerelease)


#: 注意操作符匹配顺序：长的（``>=`` / ``<=``）必须先于短的（``>`` / ``<``）。
_OPERATORS = (">", "<", ">=", "<=", "=")


def _check(version: Version, op: str, bound: Version) -> bool:
    c = compare(version, bound)
    if op == ">":
        return c > 0
    if op == "<":
        return c < 0
    if op == ">=":
        return c >= 0
    if op == "<=":
        return c <= 0
    return c == 0


def satisfies(version: str, range_str: str) -> bool:
    """判断 version 是否落在 range_str 描述的范围内。"""
    va = parse_version(version)
    for alternative in range_str.split("||"):
        matched = False
        for part in alternative.split():
            op = next((o for o in _OPERATORS if part.startswith(o)), "")
            if not op:
                op = "="
                bound_text = part
            else:
                bound_text = part[len(op):]
            if _check(va, op, parse_version(bound_text)):
                matched = True
                break
        if matched:
            return True
    return False
