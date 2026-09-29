"""INI/properties 风格的配置解析。

语法约定：
- 逐行处理，每行先去掉首尾空白再判断；空行与以 ``#`` 或 ``;`` 开头的行是注释；
- ``[name]`` 开新节，**节名要去掉首尾空白**；出现在任何节头之前的键属于
  全局节，节名为空字符串 ``""``；
- ``key = value``（``=`` 两侧空白可有可无）：**key 与 value 都要去掉首尾空白**，
  只按第一个 ``=`` 切分，value 里可以再有 ``=``；
- 类型强转（对裁剪后的 value，在 ``parse`` 时完成）：
  - ``true`` / ``yes`` / ``on``（不分大小写）→ ``True``；
    ``false`` / ``no`` / ``off``（不分大小写）→ ``False``；
  - 能构成整数的（**允许负号**）→ ``int``；
  - 其余保持 ``str``；
- ``get``：**节缺失或键缺失都返回 default**，不抛异常。
"""

from __future__ import annotations


def _coerce(value: str) -> object:
    """按约定把字符串值转成 bool / int / str。"""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.isdigit():
        return int(value)
    return value


def parse(text: str) -> dict[str, dict[str, object]]:
    """把配置文本解析成 ``{节名: {键: 值}}``。"""
    config: dict[str, dict[str, object]] = {}
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            config[section] = {}
            continue
        key, _, value = line.partition("=")
        config.setdefault(section, {})[key] = _coerce(value)
    return config


def get(
    config: dict[str, dict[str, object]],
    section: str,
    key: str,
    default: object = None,
) -> object:
    """读取 ``config[section][key]``；节或键缺失时返回 default。"""
    if section not in config:
        raise KeyError(section)
    return config[section].get(key, default)
