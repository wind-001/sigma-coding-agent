"""SSE 分帧：Anthropic 比 OpenAI **多一层 ``event:`` 类型行**。

与 ``openai/sse.py`` 的差异（为什么不能共用）
    OpenAI 的流只有 ``data:`` 行；Anthropic 的每个事件是两行——
    ``event: <类型>`` + ``data: <JSON>``。类型行携带的信息 data JSON 里的
    ``type`` 字段本来也有（Anthropic 两处都放），但**网关与代理实现未必
    完整**：只认 data 里的 ``type`` 会在缺字段的实现上拿到空事件名。
    所以两处都读、按优先级兜底。

    不共用 openai 的 ``parse_sse_line`` 还有一条结构性理由：
    ``event:`` 行**作用于下一个 data 行**——解析器必须有跨行状态，
    而那边的解析器是纯逐行函数。把状态塞进返回值只会让两个调用方都别扭。

保留的坑（与 openai 侧同源的教训）
    1. ``: keep-alive`` 注释行协议允许，必须忽略而不是报错。
    2. ``id:`` / ``retry:`` 字段本层不使用，跳过。
    3. **一行 ``data:`` 是一个完整 JSON**。SSE 规范允许一个事件的数据拆成
       多个 ``data:`` 行（用换行拼接），但 Anthropic 不这么发；真遇到时
       各行独立解析、坏的那行作为错误可见——不实现拼接器。
    4. 坏 JSON **抛出**而不返回 ``None``：返回 ``None`` 等于"这行没数据"，
       而实际是"有数据但坏了"——两者必须可区分（调用方记错误事件）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SseEvent:
    """一个已配对的 SSE 事件。

    ``event`` 是事件类型名（优先取 data JSON 的 ``type`` 字段，
    缺失时回退 ``event:`` 行）；``data`` 是解析后的 JSON——
    **类型标注是 ``Any`` 而不是 ``dict``**：``data: 123`` 这类合法 JSON
    非对象也能到这里，"不是对象"由 provider 记成错误事件
    （与 openai 侧同一处置），分帧层不做二次判定。
    """

    event: str
    data: Any


class SseEventParser:
    """逐行喂入、产出配对事件的分帧器。

    有状态：``event:`` 行的值保存到下一个 ``data:`` 行被消费为止。
    一个实例对应一条响应流，不要跨流复用。
    """

    def __init__(self) -> None:
        self._pending_event: str | None = None

    def feed(self, line: str) -> SseEvent | None:
        """喂一行，返回配好的事件；``None`` 表示这行不产生事件。"""
        stripped = line.strip()
        if not stripped:
            # 空行是 SSE 的事件边界：悬空的 event: 字段在边界后失效——
            # 不重置的话，一个没有 data 的 event: 行会串味到后面的数据行。
            self._pending_event = None
            return None
        if stripped.startswith(":"):
            # 保活注释行。协议允许，必须忽略而不是报错。
            return None
        if stripped.startswith("event:"):
            self._pending_event = stripped[len("event:") :].strip()
            return None
        if not stripped.startswith("data:"):
            # id: / retry: 等字段，本层不使用。
            return None
        data = stripped[len("data:") :].strip()
        if data == "[DONE]":
            # OpenAI 风格的终止行——Anthropic 不发，但经网关代理的流可能有。
            # 归一成 None：终止判定由 provider 的 message_stop 锚负责（G-ANTH-6），
            # 这里不充当第二个锚，避免两套口径。
            self._pending_event = None
            return None

        payload = json.loads(data)  # 坏 JSON 在这里抛，由调用方处置
        event_name = str(payload.get("type") or "") if isinstance(payload, dict) else ""
        if not event_name:
            event_name = self._pending_event or ""
        self._pending_event = None
        return SseEvent(event=event_name, data=payload)
