"""门槛 G1 / G2 / G6 / G9：抽象基类、签名保真、错误分类、SystemMessage 字段集。

为什么把这几条放一个文件
    它们都是「类型层面的约束」——不涉及网络、不涉及事件流。
    按"约束的性质"分组比按"文件"分组更容易看出门槛的意图。

对应 ``docs/plans/P1-批次1-详规.md`` 第 5 节门槛表。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sigma_ai.base import BaseProvider, CancelToken, SamplingParams, StreamOptions
from sigma_ai.errors import (
    ErrorCode,
    ProviderError,
    ProviderErrorPayload,
    classify_http_status,
)
from sigma_ai.messages import (
    AssistantMessage,
    ImageBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolMeta,
    Usage,
)

# ---------------------------------------------------------------------------
# G1：抽象基类直接实例化必须抛 TypeError
# ---------------------------------------------------------------------------


from sigma_ai.stamps import from_epoch as ts
@pytest.mark.parametrize("abstract_cls", [BaseProvider, CancelToken])
def test_abstract_base_cannot_be_instantiated(abstract_cls: type) -> None:
    """G1：忘实现抽象方法时，**实例化**就报错，而不是等到首次调用。

    这是 a2（一律继承制）相对 ``Protocol`` 的核心收益：
    Protocol 把问题推到第一次调用，ABC 在构造时就暴露。
    """
    with pytest.raises(TypeError):
        abstract_cls()  # type: ignore[abstract]


def test_incomplete_subclass_still_fails() -> None:
    """G1 的补充：只实现一半的子类同样不得实例化。

    只测基类不够——基类必抛，那是最容易通过的情形。
    真正要防的是"子类忘了实现某个抽象方法"。
    """

    class HalfDone(BaseProvider):
        def estimate_tokens(self, messages: list) -> int:  # type: ignore[type-arg]
            return 0

        # 故意不实现 stream

    with pytest.raises(TypeError):
        HalfDone()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# G2：三个签名类字段必须逐字节保真
# ---------------------------------------------------------------------------


def _build_signed_message() -> AssistantMessage:
    return AssistantMessage(
        content=[
            TextBlock(text="hello", text_signature="SIG-TEXT"),
            ThinkingBlock(
                thinking="hmm", thinking_signature="SIG-THINK", redacted=True
            ),
            ToolCallBlock(
                id="call_1",
                name="read",
                arguments={"path": "a.py"},
                thought_signature="SIG-TOOL",
            ),
        ],
        usage=Usage(prompt_tokens=10, completion_tokens=20, cached_tokens=5),
        stop_reason="tool_use",
        timestamp=ts(1_727_000_000),
    )


def test_signature_fields_survive_json_roundtrip() -> None:
    """G2：三个不透明签名字段经 JSON 往返后逐字节不变。

    为什么这条门槛值得单独存在
        这些字段是 provider 返回的、必须原样回传的不透明串。
        漏了会让多轮对话行为异常，**而且症状完全不指向根因**——
        你不会想到是这里丢的字段。
        断言很便宜，防的是一类极难排查的 bug。
    """
    original = _build_signed_message()

    once = AssistantMessage.model_validate_json(original.model_dump_json())
    twice = AssistantMessage.model_validate_json(once.model_dump_json())

    text_block = twice.content[0]
    thinking_block = twice.content[1]
    tool_block = twice.content[2]

    assert isinstance(text_block, TextBlock)
    assert isinstance(thinking_block, ThinkingBlock)
    assert isinstance(tool_block, ToolCallBlock)

    assert text_block.text_signature == "SIG-TEXT"
    assert thinking_block.thinking_signature == "SIG-THINK"
    assert thinking_block.redacted is True
    assert tool_block.thought_signature == "SIG-TOOL"


def test_signature_fields_survive_as_none() -> None:
    """G2 的反面：没有签名时不得被填成空字符串。

    ``None`` 与 ``""`` 在协议上不是一回事——某些 provider 会拒绝空签名串。
    如果序列化把 ``None`` 变成 ``""``，症状同样是难排查的多轮对话异常。
    """
    message = AssistantMessage(
        content=[TextBlock(text="no signature")],
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="stop",
        timestamp=ts(1),
    )
    back = AssistantMessage.model_validate_json(message.model_dump_json())
    block = back.content[0]
    assert isinstance(block, TextBlock)
    assert block.text_signature is None


def test_content_block_discriminated_by_type() -> None:
    """四个内容块靠 ``type`` 字段正确还原。

    这不是形式检查：判别错了会把 thinking 块当文本块，导致 signature 丢失。
    """
    message = _build_signed_message()
    back = AssistantMessage.model_validate_json(message.model_dump_json())

    types = [type(block).__name__ for block in back.content]
    assert types == ["TextBlock", "ThinkingBlock", "ToolCallBlock"]


def test_image_block_roundtrip() -> None:
    """ImageBlock 是四个块里唯一没在其他测试里出现的，单独覆盖。"""
    message = AssistantMessage(
        content=[ImageBlock(data="aGVsbG8=", mime_type="image/png")],
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="stop",
        timestamp=ts(1),
    )
    back = AssistantMessage.model_validate_json(message.model_dump_json())
    block = back.content[0]
    assert isinstance(block, ImageBlock)
    assert block.data == "aGVsbG8="
    assert block.mime_type == "image/png"


# ---------------------------------------------------------------------------
# G9：SystemMessage 字段集合被钉死，防止摘要塞进来
# ---------------------------------------------------------------------------


def test_system_message_field_set_is_frozen() -> None:
    """G9：``SystemMessage`` 的字段集合必须**恰好**是约定的那些。

    为什么这条断言重要
        ``SystemMessage`` 有三个字段（``sections`` / ``tools_added`` /
        ``tools_removed``），让它看起来"可以承载任何想注入上下文的东西"。
        实施时把**压缩摘要**拼进 ``content`` 是非常自然的选择——
        因为没有任何东西阻止这么做。

        但摘要必须降级成 ``user``（架构方案 4.0.6 节）：system 消息进常驻区，
        而常驻区在会话内必须逐字节稳定（D4）。中途变化会让 prompt cache
        从变动点起全部失效，**症状要等到 P2 才以"成本异常上升"的形式暴露**。

        所以这条断言的作用是：**任何人想给 SystemMessage 加字段（比如
        ``summary``），必须先看到这个失败并想清楚。**
    """
    expected = {
        "role",
        "content",
        "sections",
        "tools_added",
        "tools_removed",
        "timestamp",
    }
    actual = set(SystemMessage.model_fields)
    assert actual == expected, (
        f"SystemMessage 的字段集合被改动了。\n"
        f"新增：{actual - expected}\n"
        f"删除：{expected - actual}\n"
        f"若新增字段与压缩摘要有关，请先读架构方案 4.0.6 节——"
        f"摘要必须走 UserMessage，不能进 system。"
    )


def test_system_message_still_supports_prompt_evolution() -> None:
    """G9 的配套：钉字段集合的同时，提示词演进能力必须仍在。

    只钉不测会把"防摘要"变成"防一切"，那就偏离了原意。
    """
    message = SystemMessage(
        content="base prompt",
        sections={"persona": "be careful", "tone": None},
        tools_added=[ToolMeta(name="read", description="read a file")],
        tools_removed=["bash"],
        timestamp=ts(1),
    )
    back = SystemMessage.model_validate_json(message.model_dump_json())

    assert back.sections == {"persona": "be careful", "tone": None}
    assert back.tools_added is not None
    assert back.tools_added[0].name == "read"
    assert back.tools_removed == ["bash"]


# ---------------------------------------------------------------------------
# G6：context_overflow 必须能被识别
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code",
    [429, 401, 403, 500, 502, 503, 504, 400, 404],
)
def test_classify_http_status_returns_known_code(status_code: int) -> None:
    """G6 的基础：状态码必须被映射到已定义的五类码之一。

    ``classify_http_status`` 的返回值被用作 ``ErrorCode``，
    若某个分支返回了未定义的值，会在构造 payload 时才炸。
    """
    code = classify_http_status(status_code)
    assert isinstance(code, ErrorCode)


@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum context length is 8192 tokens",
        "prompt is too long: 9000 tokens > 8000 maximum",
        "Please reduce the length of the messages",
    ],
)
def test_context_overflow_is_detected_from_message(message: str) -> None:
    """G6 的核心：``400`` + 特定文案必须被识别成 ``context_overflow``。

    为什么这条重要
        它是压缩机制的**两条触发路径之一**（另一条是本地估算超阈值）。
        识别逻辑属于 provider 层，不属于压缩层——所以 P1 就要做对，
        哪怕 P1 还没有压缩。

        漏判的后果是：上下文真的超限时，压缩不会触发，
        任务直接失败，而错误信息看起来只是"请求非法"。
    """
    assert classify_http_status(400, message) is ErrorCode.CONTEXT_OVERFLOW


def test_plain_400_is_invalid_request_not_overflow() -> None:
    """G6 的反面：普通 400 不得被误判成超限。

    误判的后果是反向的：会触发一次毫无必要的压缩，
    把正常上下文压掉，白花 token 还丢信息。
    """
    code = classify_http_status(400, "invalid parameter: temperature must be < 2")
    assert code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.RATE_LIMIT, True),
        (ErrorCode.TRANSIENT, True),
        (ErrorCode.CONTEXT_OVERFLOW, False),
        (ErrorCode.AUTH, False),
        (ErrorCode.INVALID_REQUEST, False),
    ],
)
def test_retriable_is_derived_from_code(code: ErrorCode, expected: bool) -> None:
    """``retriable`` 必须由 ``code`` 派生，不能手填。

    手填就会填错，而「该重试的没重试」和「不该重试的狂重试」都是线上事故。
    """
    payload = ProviderErrorPayload(code=code, message="x")
    assert payload.retriable is expected

    exc = ProviderError(code, "x")
    assert exc.retriable is expected


def test_provider_error_payload_roundtrip() -> None:
    """错误必须是**可序列化的数据**，否则事件流无法落盘与回放。

    这条断言对应实施时发现的一个真实缺陷：原设计让 ``ErrorEvent``
    直接持有 ``ProviderError``（异常），Pydantic 无法为它生成 schema。
    修法不是开 ``arbitrary_types_allowed``（那只是让报错消失），
    而是把"数据"与"可抛出"分成两个类型。
    """
    original = ProviderErrorPayload(
        code=ErrorCode.RATE_LIMIT,
        message="slow down",
        raw={"error": {"message": "slow down"}},
        status_code=429,
    )
    back = ProviderErrorPayload.model_validate_json(original.model_dump_json())

    assert back.code is ErrorCode.RATE_LIMIT
    assert back.message == "slow down"
    assert back.raw == {"error": {"message": "slow down"}}
    assert back.status_code == 429
    assert back.retriable is True


def test_provider_error_from_payload_preserves_info() -> None:
    """``ProviderError.from_payload`` 是回放路径：异常必须与数据同源。

    ``str(exc)`` 与 ``payload`` 的信息必须完全一致——
    数据只有一份，异常只是它的可抛出外壳。
    """
    payload = ProviderErrorPayload(
        code=ErrorCode.CONTEXT_OVERFLOW, message="too long", status_code=400
    )
    exc = ProviderError.from_payload(payload)

    assert exc.code is payload.code
    assert exc.message == payload.message
    assert exc.status_code == payload.status_code
    assert "context_overflow" in str(exc)
    assert "400" in str(exc)


# ---------------------------------------------------------------------------
# 附带：请求参数模型的基本约束
# ---------------------------------------------------------------------------


def test_stream_options_include_usage_defaults_true() -> None:
    """``include_usage`` 必须默认为 ``True``。

    默认 ``False`` 的后果：流里没有 usage，用量统计只能靠估算——
    而估算数不能进评测报告（详规 4.1 节）。默认值错会让这个坑静默存在。
    """
    assert StreamOptions().include_usage is True


def test_sampling_params_allow_all_none() -> None:
    """采样参数允许全空——此时上层不该往请求体里塞这些字段。

    （"不塞 null"的行为在网络层测试里断言。）
    """
    params = SamplingParams()
    assert params.temperature is None
    assert params.max_tokens is None
    assert params.top_p is None


def test_assistant_message_requires_usage_and_stop_reason() -> None:
    """``usage`` 与 ``stop_reason`` 是必填——缺了说明 provider 层漏了信息。

    让它们有默认值会掩盖"某条路径忘了填"，那是评测数据不可信的开始。
    """
    with pytest.raises(ValidationError):
        AssistantMessage(content=[TextBlock(text="x")], timestamp=ts(1))  # type: ignore[call-arg]
