"""SSE 分帧与行解析。

**这条链路上最容易出事、也最难复现的一环**（详见编排笔记里的"四个坑"）：

1. SSE 的分块边界可能切开一个 JSON——必须缓冲累积，不能逐块解析。
   悄悄丢字符，症状随机且不指向根因。
2. ``[DONE]`` **不是 JSON**——遇到它直接结束，不要送去 ``json.loads``。
3. ``: keep-alive`` 这类注释行协议允许，必须忽略而不是报错。
4. ``event:`` / ``id:`` / ``retry:`` 字段本层不使用，跳过。
"""

from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """解析一行 SSE。

    返回 ``None`` 表示"这行不产生数据"（空行、注释、``[DONE]``）。

    **``[DONE]`` 不是 JSON**——这是详规 2.7 节列的坑第 2 条。
    送去 ``json.loads`` 会抛异常，被误当成协议错误。
    """
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith(":"):
        # SSE 注释行，用于保活。协议允许，必须忽略而不是报错。
        return None
    if not stripped.startswith("data:"):
        # event: / id: / retry: 等字段，本层不使用。
        return None
    data = stripped[len("data:") :].strip()
    if data == "[DONE]":
        return None
    return json.loads(data)  # type: ignore[no-any-return]


def is_done_line(line: str) -> bool:
    """判断一行是否是 ``data: [DONE]`` 终止行。

    ``parse_sse_line`` 把 ``[DONE]`` 归一成 ``None``（与空行、注释同构），
    调用方因此**无法区分**"正常终止"与"什么都没说"——而断流检测
    （provider 的截断可见性）恰恰需要这个区分。所以终止信号单独开一个判据。
    """
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return False
    return stripped[len("data:") :].strip() == "[DONE]"


