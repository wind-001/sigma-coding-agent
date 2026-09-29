"""``semver_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import semver_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from semver_lite import Version, compare, parse_version, satisfies

# semver.org §11 的优先级链示例（含本仓库扩展的数字标识符情形）。
ORDERED = [
    "1.0.0-2",
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.9",
    "1.0.0-alpha.10",
    "1.0.0-beta",
    "1.0.0",
    "1.0.1",
    "1.2.0",
]


# --- 解析 --------------------------------------------------------------------


def test_parse_basic() -> None:
    v = parse_version("1.2.3")
    assert v == Version(major=1, minor=2, patch=3, prerelease=())


def test_parse_prerelease() -> None:
    v = parse_version("1.2.3-alpha.1")
    assert v.prerelease == ("alpha", "1")


def test_parse_rejects_two_parts() -> None:
    with pytest.raises(ValueError):
        parse_version("1.2")


# --- 比较 --------------------------------------------------------------------


def test_release_beats_prerelease() -> None:
    """正式版高于同号带 prerelease 的版本。"""
    assert compare("1.2.3", "1.2.3-alpha.1") == 1


def test_numeric_identifier_compares_numerically() -> None:
    """数字标识符按数值比：9 < 10。"""
    assert compare("1.0.0-alpha.10", "1.0.0-alpha.9") == 1


def test_numeric_identifier_below_alphanumeric() -> None:
    """纯数字标识符低于含字母的标识符。"""
    assert compare("1.0.0-1", "1.0.0-alpha") == -1


def test_shorter_prerelease_is_lower() -> None:
    assert compare("1.0.0-alpha", "1.0.0-alpha.1") == -1


def test_equal_versions() -> None:
    assert compare("1.2.3", "1.2.3") == 0


def test_priority_chain_is_total_order() -> None:
    for lower, higher in zip(ORDERED, ORDERED[1:]):
        assert compare(lower, higher) == -1, f"{lower} 应低于 {higher}"


# --- 范围匹配 ------------------------------------------------------------------


def test_satisfies_exact() -> None:
    assert satisfies("1.2.3", "1.2.3")
    assert satisfies("1.2.3", "=1.2.3")
    assert not satisfies("1.2.4", "1.2.3")


def test_satisfies_ge() -> None:
    assert satisfies("1.5.0", ">=1.2.0")
    assert not satisfies("1.0.0", ">=1.2.0")


def test_satisfies_le() -> None:
    assert satisfies("1.0.0", "<=1.2.0")
    assert not satisfies("1.3.0", "<=1.2.0")


def test_satisfies_range_is_and() -> None:
    """空格分隔的比较子是 AND：两边都满足才满足。"""
    assert satisfies("1.5.0", ">=1.0.0 <2.0.0")
    assert not satisfies("2.5.0", ">=1.0.0 <2.0.0")
    assert not satisfies("0.9.0", ">=1.0.0 <2.0.0")


def test_satisfies_or() -> None:
    assert satisfies("3.0.0", "<1.0.0 || >=3.0.0")
    assert not satisfies("2.0.0", "<1.0.0 || >=3.0.0")


def test_satisfies_prerelease_in_range() -> None:
    """范围比较同样走完整 semver 规则（含 prerelease）。"""
    assert satisfies("1.5.0-beta.2", ">=1.5.0-beta.1 <1.5.0")
    assert not satisfies("1.5.0-beta.2", ">=1.5.0 <2.0.0")
