"""POSIX 风格路径的字符串规范化(纯字符串处理,不碰文件系统)。

约定:
- ``normalize(path)``:
  - 折叠连续的 ``/``,丢弃空的段与 ``.`` 段;
  - ``..`` 弹掉上一段;对**绝对路径**,到根之后再出现的 ``..`` 一律丢弃
    (``/a/../../b`` 是 ``/b``);对**相对路径**,越界的 ``..`` 保留在开头
    (``../a`` 还是 ``../a``);
  - 结果:绝对路径以 ``/`` 开头(根是 ``/``);空串与 ``.`` 规范化成 ``.``;
- ``join(*parts)``:依次拼接,**遇到绝对片段就重置前缀**;拼完按 normalize
  规则规范化;无参数返回空串;
- ``relative_to(base, target)``:target 相对 base 的路径;要求两者同为绝对
  或同为相对,且 target 落在 base 之下(或与之相等,返回 ``.``),
  否则抛 ``ValueError``;前缀判定必须**按段比较**(``/a/bc`` 不在 ``/a/b`` 之下);
- ``is_under(base, target)``:等价于 relative_to 不抛 ``ValueError``。
"""

from __future__ import annotations


def normalize(path: str) -> str:
    """按上述规则规范化路径字符串。"""
    absolute = path.startswith("/")
    out: list[str] = []
    for part in path.split("/"):
        if part == ".":
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    result = "/".join(out)
    if absolute:
        return "/" + result
    return result or "."


def join(*parts: str) -> str:
    """拼接路径片段;绝对片段重置前缀。"""
    result = ""
    for part in parts:
        if part.startswith("/"):
            result = part
        elif result == "":
            result = part
        else:
            result = result + "/" + part
    return result


def relative_to(base: str, target: str) -> str:
    """target 相对 base 的路径;不满足前提抛 ValueError。"""
    base_n = normalize(base)
    target_n = normalize(target)
    if base_n.startswith("/") != target_n.startswith("/"):
        raise ValueError(f"绝对与相对路径无法互相相对:{base!r} -> {target!r}")
    if not target_n.startswith(base_n):
        raise ValueError(f"{target!r} 不在 {base!r} 之下")
    rest = target_n[len(base_n):].lstrip("/")
    return rest or "."


def is_under(base: str, target: str) -> bool:
    """target 是否落在 base 之下(或相等)。"""
    try:
        relative_to(base, target)
    except ValueError:
        return False
    return True
