"""极简模板渲染：把 ``{{key}}`` 换成对应的值。

只做替换，不做转义与过滤器。两条设计立场：

- 占位符**互相独立**：``{{a}} 和 {{b}}`` 是两个占位符，不是一段文本；
- **缺失的 key 是调用方的 bug**：宁可在渲染时抛 ``KeyError``，
  也不要渲染出一份悄悄缺了字段的文案（那要等到用户手里才被发现）。

key 的字符集与 Python 标识符一致（字母数字下划线）。
"""

from __future__ import annotations

import re

_PATTERN = re.compile(r"{{(.+)}}")


def render(template: str, **variables: object) -> str:
    """把 ``template`` 里的占位符替换掉，返回渲染结果。"""

    def _replace(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(variables.get(key, ""))

    return _PATTERN.sub(_replace, template)
