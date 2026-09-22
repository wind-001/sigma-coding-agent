"""门槛 G25：`ToolResultAgentMessage.from_result()` 的字段传递。

**这条兑现批次 1.5 详规第 9 节的 S1**——当时因为 `ToolResult` 类型
还不存在（属批次 2），所以只登记、不实现。现在补上并以断言钉住。

为什么值得一个独立文件
    `from_result` 是**工具结果进入会话的唯一入口**。
    它漏传字段**不会报错**（Pydantic 有默认值），只会让信息静默消失：

    | 字段 | 漏掉的后果 | 会不会报错 |
    | --- | --- | --- |
    | `content` | 模型看不到工具输出 | 会（必填） |
    | `details` | **审计与评测数据消失** | **不会** |
    | `is_error` | 失败被当成成功 | 会（有默认值但语义错） |

    中间那行才是这条门槛真正在防的东西——它是最容易无声丢失的一个。
"""

from __future__ import annotations

import pytest

from sigma_agent.agent_messages import ToolResultAgentMessage
from sigma_agent.types import ToolResult
from sigma_ai.messages import TextBlock, ToolCallBlock

from sigma_ai.stamps import from_epoch as ts
FIXED_TS = ts(1_700_000_000)


def _call(**kwargs: object) -> ToolCallBlock:
    base: dict[str, object] = {"id": "call_1", "name": "echo", "arguments": {}}
    base.update(kwargs)
    return ToolCallBlock(**base)  # type: ignore[arg-type]


def test_from_result_propagates_every_field() -> None:
    """content / details / is_error 必须一个不落地传递到消息上。"""
    result = ToolResult(
        content=[TextBlock(text="工具输出")],
        details={"exit_code": 0, "bytes": 12},
        is_error=False,
    )

    message = ToolResultAgentMessage.from_result(
        _call(), result, timestamp=FIXED_TS
    )

    assert message.tool_call_id == "call_1"
    assert message.tool_name == "echo"
    assert message.content[0].text == "工具输出"  # type: ignore[union-attr]
    assert message.details == {"exit_code": 0, "bytes": 12}
    assert message.is_error is False
    assert message.timestamp == FIXED_TS


def test_from_result_preserves_error_flag() -> None:
    """`is_error=True` 必须原样传递。

    看上去是废话，但它决定了「纠错增益」能不能被观测：
    失败若被记成成功，模型不会重试，而评测报告里的成功率会虚高。
    """
    result = ToolResult(
        content=[TextBlock(text="stderr: 命令失败")],
        details={"exit_code": 1},
        is_error=True,
    )
    message = ToolResultAgentMessage.from_result(_call(), result, timestamp=FIXED_TS)
    assert message.is_error is True


def test_exclusion_is_caller_decision_not_tool_result() -> None:
    """`exclude_from_context` 由**调用方**决定，**不从 `ToolResult` 读**。

    R3 已确认：它不上提到基类（批次 1.5 的 W4），也不从工具结果读——
    **工具不该知道"这条消息会不会进上下文"**，那是调用层的判断。

    这条断言钉住的是：默认不排除，且能显式排除。
    """
    result = ToolResult(content=[TextBlock(text="x")])

    plain = ToolResultAgentMessage.from_result(_call(), result, timestamp=FIXED_TS)
    assert plain.exclude_from_context is False
    assert plain.to_llm() is not None  # 默认进上下文

    excluded = ToolResultAgentMessage.from_result(
        _call(),
        result,
        exclude_from_context=True,
        exclude_reason="输出太大",
        timestamp=FIXED_TS,
    )
    assert excluded.exclude_from_context is True
    assert excluded.exclude_reason == "输出太大"
    # 被排除 → 降级时**完全消失**，不是变成一条空消息
    assert excluded.to_llm() is None


def test_timestamp_defaults_when_omitted() -> None:
    """不传 `timestamp` 时用当前时间——但**回放测试必须显式传**。

    这条用例本身用一个宽松断言（时间戳 > 0），
    因为它的作用只是确认"不传也不会崩"。
    需要确定性时用 `timestamp=` 参数（loop 里就是这么做的）。
    """
    message = ToolResultAgentMessage.from_result(
        _call(), ToolResult(content=[TextBlock(text="x")])
    )
    # 时间戳现在是可读字符串（sigma_ai.stamps），**不能再比大小**。
    # 要验的是"它被填上了"，用非空即可；格式本身由 stamps 的用例钉。
    assert isinstance(message.timestamp, str) and message.timestamp


@pytest.mark.parametrize("missing", ["details", "is_error"])
def test_omitted_optional_fields_do_not_crash(missing: str) -> None:
    """可选字段不传时不能崩——`ToolResult` 本身有默认值。

    这是 `from_result` 的健壮性底线：工具可以只给 `content`。
    """
    result = ToolResult(content=[TextBlock(text="x")])
    message = ToolResultAgentMessage.from_result(_call(), result, timestamp=FIXED_TS)
    assert getattr(message, missing) is not None or missing == "details"
