"""交互会话的测试（门槛 G35）。

G35 要证明的是**跨轮累积**：第二轮提问时，第一轮的工具结果还在上下文里。
这条如果失守，交互模式就退化成"每轮都是全新会话"——
症状是模型反问"你说的那个文件是哪个？"，而用户完全不知道发生了什么。

这里用**真实的内置 read 工具**（不是自造的假工具）：
跨轮丢历史的 bug 往往出在"工具结果消息怎么回流"上，用真工具才测得到。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sigma.sdk import InteractiveSession
from sigma_agent.agent_messages import ToolResultAgentMessage
from sigma_ai.fake import FakeProvider


def _read_call(path: str = "a.txt") -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": "call_1",
            "name": "read",
            "arguments_delta": f'{{"path": "{path}"}}',
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _text(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_history_survives_across_turns(tmp_path: Path) -> None:
    """门槛 G35：第一轮的工具结果**同一实例**在第二轮之后仍在历史里。

    用 ``is`` 而不是 ``==``：要证明的是"没有丢"，不是"内容恰好一样"。
    """
    root = tmp_path
    (root / "a.txt").write_text("hello\n", encoding="utf-8")

    provider = FakeProvider.from_rounds(
        [_read_call(), _text("读完了"), _text("是 hello")]
    )
    session = InteractiveSession(
        provider=provider, workspace_root=root, model="fake"
    )

    first = await session.send("读一下 a.txt")
    tool_msg = next(
        m for m in first.messages if isinstance(m, ToolResultAgentMessage)
    )
    # 工具真的读到了内容——否则下面"还在历史里"证明的是一条空结果的留存
    assert any("hello" in getattr(b, "text", "") for b in tool_msg.content)

    await session.send("刚才读到的是什么？")

    assert any(
        m is tool_msg for m in session.history()
    ), "第一轮的工具结果不在历史里——会话每轮都在重建，交互模式失去意义"
