"""``ToolCallAssembler`` 的测试（2026-09-24 review 修复补建）。

此前装配器只有经 loop 的间接覆盖。本文件钉住的契约：
**``thought_signature`` 必须原样透传**——它是 provider 返回的不透明串
（messages.py 模块文档明确要求"必须原样回传"），装配器丢弃它，
``ToolCallBlock.thought_signature`` 字段就成了摆设。
"""

from __future__ import annotations

from sigma_ai.events import ToolCallDelta
from sigma_ai.tool_calls import ToolCallAssembler


def test_thought_signature_survives_assembly() -> None:
    """带签名的分片装配后，``to_block().thought_signature`` 与喂入值一致。"""
    assembler = ToolCallAssembler()
    assembler.feed(
        ToolCallDelta(
            index=0,
            id="c1",
            name="read",
            arguments_delta='{"path": "a.py"}',
            thought_signature="SIG-1",
        )
    )
    calls = assembler.finish()

    assert len(calls) == 1
    assert calls[0].ok
    assert calls[0].thought_signature == "SIG-1"
    assert calls[0].to_block().thought_signature == "SIG-1"


def test_thought_signature_later_non_empty_delta_wins() -> None:
    """签名只出现在**后到的**分片上时也要被收到（覆盖规则与 id/name 一致）。"""
    assembler = ToolCallAssembler()
    assembler.feed(ToolCallDelta(index=0, id="c1", name="read", arguments_delta="{"))
    assembler.feed(
        ToolCallDelta(index=0, arguments_delta="}", thought_signature="SIG-late")
    )
    calls = assembler.finish()

    assert calls[0].ok
    assert calls[0].to_block().thought_signature == "SIG-late"


def test_no_signature_stays_none() -> None:
    """回归：没有签名的普通调用保持 ``None``，不被填成空串。"""
    assembler = ToolCallAssembler()
    assembler.feed(ToolCallDelta(index=0, id="c1", name="read", arguments_delta="{}"))
    calls = assembler.finish()

    assert calls[0].to_block().thought_signature is None
