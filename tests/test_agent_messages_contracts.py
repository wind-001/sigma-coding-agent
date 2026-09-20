"""门槛 G17 / G18：编解码的硬失败语义，与"唯一引用点"的边界。

**G17：为什么"未知 role 抛错"要单独一个门槛**

    它与 G14（`convert_to_llm` 遇未知类型发 warning）**看起来矛盾**：

    | 场景 | 处置 |
    | --- | --- |
    | `convert_to_llm` 遇到未注册的消息类型 | warning + 丢弃（运行期容错） |
    | `message_from_dict` 遇到未注册的 role | **抛错**（落盘数据不可信） |

    **不矛盾**：前者是"这条消息暂时没法处理"（可能是扩展加载顺序问题，
    或消息只是给 UI 看的）；后者是"这份数据坏了"
    （role 存在就意味着**曾经**有类型注册过它，现在没有 = 数据与代码不匹配）。

    这两条语义的区分必须在**代码与测试里都有体现**——
    只在 docstring 里写一句"这样设计是有意的"，下一个人会当成疏忽顺手改掉。

    注入 E17 会把 `message_from_dict` 改成返回 `None`，本文件会红。

**G18：为什么不新增契约文件**

    详规第 1 节已实测确认：现有的 `layers` 契约
    （`sigma` / `sigma_tools` / `sigma_session` / `sigma_agent` / `sigma_ai`）
    **已经覆盖** `sigma_ai → sigma_agent` 这个方向——
    `layers` 禁止下层引用上层，而 `sigma_ai` 就在最下层。

    所以本文件要做的不是"新增契约"，而是**证明既有契约确实覆盖了它**：
    把 `sigma_ai` 里的违规 import 造出来，确认它变红。
    这就是 E18 的真实形态（留在本文件里，而不是只放在注入脚本里——
    前者每次跑测试都在验证，后者只在手动执行时验证一次）。

对应 ``docs/plans/P1-批次1.5-详规.md`` 第 5 节门槛 G17 / G18。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sigma_agent.agent_messages import (
    AgentMessage,
    LlmMessageWrapper,
    MessageDecodeError,
    ToolResultAgentMessage,
    UnknownMessageType,
    message_from_dict,
    message_to_dict,
    messages_from_jsonl,
    messages_to_jsonl,
    register_message_type,
)
from sigma_ai.messages import (
    AssistantMessage,
    LlmMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _tool_result_agent_message() -> ToolResultAgentMessage:
    """一条字段全填的工具结果消息——漏字段的往返测试等于没测。"""
    return ToolResultAgentMessage(
        timestamp=2000,
        tool_call_id="call-9",
        tool_name="bash",
        content=[TextBlock(text="stdout...", text_signature="sig-tool-text")],
        details={"exit_code": 0, "duration_ms": 123},
        is_error=False,
        exclude_from_context=True,
        exclude_reason="全量输出已在 details 里",
    )


# ---------------------------------------------------------------------------
# G17：未知 role 必须硬失败
# ---------------------------------------------------------------------------


def test_unknown_role_raises_on_decode() -> None:
    """G17 本体：未知 `role` **抛错**，不返回 `None`、不跳过。

    返回 `None` 会让调用方只能"检查 None"——而漏检查就变成静默丢弃，
    正是本设计要避免的模式（与 `get_message_type` 同一理由）。

    注入 E17 会把它改成返回 `None`，本断言会红。
    """
    payload = {"role": "test_codec_never_registered", "timestamp": 1}

    with pytest.raises(UnknownMessageType, match="test_codec_never_registered"):
        message_from_dict(payload)


def test_non_string_role_raises() -> None:
    """`role` 缺失或不是字符串时同样抛错——**不 fallback 到任何默认类型**。

    fallback 是最诱人的错误：`payload.get("role", "llm")` 看起来"更健壮"，
    实际会把一条结构不明的数据解析成一个字段碰巧对得上的 LLM 消息，
    比抛错危险得多。

    `None` / 数字 / 缺字段三种形态都测——它们走的是同一条分支，
    但失败原因不同，值得分开钉住。
    """
    for payload in (
        {"timestamp": 1},
        {"role": None, "timestamp": 1},
        {"role": 42, "timestamp": 1},
    ):
        with pytest.raises(UnknownMessageType):
            message_from_dict(payload)  # type: ignore[arg-type]


def test_broken_json_line_raises_with_line_number() -> None:
    """坏 JSON 抛错且**带行号**——否则长 JSONL 里无从定位。

    这条与"未知 role 抛错"合起来，构成"落盘数据的问题必须早暴露"
    这个完整语义：不静默跳过的**任何**一行的**任何**一类问题。

    **第一行必须是合法的**，否则测出的是"第 1 行坏了"——
    那样这条测试就退化成"随便哪行坏了都会报"，**证明不了行号是对的**。
    所以第 1 行用一条真实的、能往返成功的消息。
    """
    good = LlmMessageWrapper(timestamp=1, message=UserMessage(content="ok", timestamp=1))
    text = messages_to_jsonl([good]) + "\n{ 这不是 JSON"

    with pytest.raises(MessageDecodeError, match="第 2 行"):
        messages_from_jsonl(text)


def test_schema_violation_raises_decode_error_with_line_number() -> None:
    """**结构合法但字段不合法**的那一行，同样抛 `MessageDecodeError` 且带行号。

    **这条是 2026-09-20 修正掉的一个真实缺陷的回归测试。**

    修正前：`message_from_dict` 直接调 `cls.model_validate(payload)`，
    坏字段会让 `pydantic.ValidationError` **裸抛**——
    不带行号、不在本模块的异常家族里，调用方还得从
    "12 validation errors for LlmMessageWrapper..." 这种多行消息里找根因。

    危害是**错误处理会漏**：调用方为了做"这份数据坏了"这一件事，
    得同时 catch 本模块异常、`ValidationError`、`JSONDecodeError`。
    需要处理的异常种类多到会漏，就等于没有错误处理。
    """
    good = LlmMessageWrapper(timestamp=1, message=UserMessage(content="ok", timestamp=1))
    # 第 2 行 role 已知、JSON 合法，但 message 字段结构不对
    text = messages_to_jsonl([good]) + '\n{"role": "llm", "timestamp": 1, "message": {"role": "system"}}'

    with pytest.raises(MessageDecodeError, match="第 2 行"):
        messages_from_jsonl(text)


def test_json_parse_failure_is_reported_before_schema_failure() -> None:
    """一行同时"不是 JSON"和"字段不对"时，报的是**不是 JSON**。

    顺序不是随意的：JSON 都解析不出来，就没有"字段"可言。
    先报 JSON 错误能把排查方向指向"文件被截断/编码损坏"，
    而不是误导到"schema 变了"。
    """
    text = '{"role": "llm", "timestamp": 1'

    with pytest.raises(MessageDecodeError, match="不是合法 JSON"):
        messages_from_jsonl(text)


# ---------------------------------------------------------------------------
# 编解码往返
# ---------------------------------------------------------------------------


def test_tool_result_roundtrip_is_field_exact() -> None:
    """往返后**全字段相等**，用 `model_dump()` 比对而不是逐字段 assert。

    逐字段 assert 的问题是：新增字段时它**自动漏测**
    （写测试的人得记得补一行）。全字段比对对新增字段自动生效。

    **注意 `role` 的处理**：`message_to_dict` 里 role 由注册表**反查**写入
    （详规 2.2 一：注册时给的 role 才是事实来源），
    所以它在往返后仍然一致。这条一并把它钉住。
    """
    original = _tool_result_agent_message()

    restored = message_from_dict(message_to_dict(original))

    assert restored.model_dump() == original.model_dump()


def test_wrapper_roundtrip_preserves_signatures() -> None:
    """包着 LLM 消息的 wrapper 往返后，`*_signature` 逐字节不变。

    这条与批次 1 的 G2 不同：G2 测的是 LLM 层**自己**的序列化，
    这里测的是**包了一层之后**还能不能透到内层。
    Pydantic 的嵌套模型序列化在这里有一个真实的失败模式：
    内层的可选字段若被外层配置（如 `exclude_none`）影响就会消失。
    """
    inner = AssistantMessage(
        content=[TextBlock(text="正文", text_signature="sig-text")],
        api="openai-completions",
        provider="deepseek",
        model="deepseek-chat",
        response_id="resp-42",
        usage={"prompt_tokens": 7, "completion_tokens": 3, "cached_tokens": 5},  # type: ignore[arg-type]
        stop_reason="stop",
        timestamp=1000,
    )
    original = LlmMessageWrapper(timestamp=1000, message=inner)

    restored = message_from_dict(message_to_dict(original))

    assert isinstance(restored, LlmMessageWrapper)
    assert isinstance(restored.message, AssistantMessage)
    block = restored.message.content[0]
    assert isinstance(block, TextBlock)
    assert block.text_signature == "sig-text"
    assert restored.message.response_id == "resp-42"


def test_role_is_written_from_instance_field() -> None:
    """落盘里的 `role` 是实例上那个字段的值——**它就是注册时传入的值**。

    **这条是 2026-09-20 修正后的形态**，修正的是一开始写错的断言。

    本来这条叫 `test_role_is_written_from_registry_not_instance`，
    断言"role 由注册表反查、与实例字段无关"。**跑起来才发现实现做不到、
    而且规范也不要求做到**：`message_to_dict` 必然要读
    `message.role` 才能知道该往哪个字典项写，唯一的事实来源只能是那个字段。

    修正后的事实是：

    - 规范（本模块 docstring）要求**字段默认值 == 注册用的 role**，
      这是一条给写代码的人看的约定；
    - 代码不做"两人不一致时以注册表为准"的额外防御
      （YAGNI，见 `message_to_dict` docstring）；
    - 一致性由 `message_from_dict` 在**反序列化时**兜底：
      role 查不到类就抛 `UnknownMessageType`。

    **这条断言改动的意义大于它测的东西**：它记录了一次
    "测试暴露的是设计写错了，不是实现写错了"。
    全绿的测试套件里，这种改动必须留下痕迹，否则下一个人会以为
    第一版断言才是意图。
    """

    @register_message_type(role="test_codec_role_source")
    class _Custom(AgentMessage):
        role: str = "test_codec_role_source"
        text: str = ""

        def to_llm(self) -> LlmMessage | None:
            return None

    payload = message_to_dict(_Custom(timestamp=1, text="x"))

    assert payload["role"] == "test_codec_role_source"
    # 往返后仍是同一个 role（反序列化时按它查表成功）
    assert isinstance(message_from_dict(payload), _Custom)


def test_mismatched_role_field_is_rejected_at_encode() -> None:
    """**实例字段值与注册 role 不一致时，编码直接抛错。**

    这是上一条"不做额外防御"的**另一半**：代码不试图挽救一个
    违规的输入，而是判它不合法。判定的方式是
    "拿字段值查注册表，查到的类必须恰好是这个类"。

    为什么值得写：不写的话，"违规输入会发生什么"是**未定义的**。
    未定义的行为在下一次重构时可能变成静默错误写入
    （例如查到了父类，于是子类字段被存成一个空字典）。
    """

    @register_message_type(role="test_codec_mismatch")
    class _Mismatched(AgentMessage):
        # 故意声明一个与注册 role 不同的字段默认值——违反本模块的约定
        role: str = "**故意写错**"
        text: str = ""

        def to_llm(self) -> LlmMessage | None:
            return None

    with pytest.raises(UnknownMessageType):
        message_to_dict(_Mismatched(timestamp=1, text="x"))


def test_unregistered_subclass_cannot_be_encoded() -> None:
    """**未注册**的类型在编码时就抛错——不等到反序列化才炸。

    一条"能构造但存不下去"的消息是设计错误。早暴露的收益：
    扩展作者在第一次构造时就看到问题，而不是在会话落盘、
    重开、加载时收到一个语焉不详的反序列化异常。
    """

    class NotRegistered(AgentMessage):
        role: str = "test_codec_not_registered"
        text: str = ""

        def to_llm(self) -> LlmMessage | None:
            return None

    with pytest.raises(UnknownMessageType):
        message_to_dict(NotRegistered(timestamp=1, text="x"))


def test_subclass_of_registered_type_cannot_be_encoded() -> None:
    """**子类的子类**同样不能编码——注册的是那个确切的类。

    判定用的是 `_MESSAGE_TYPES.get(role) is not type(message)`。
    改成 `isinstance` 会静默放行子类，而子类可能有额外字段——
    那些字段存下去之后读回来会**被父类的模型丢掉**，
    症状是"数据缺字段"，离根因很远。

    这条断言用的是"继承但没注册"这个最容易被 `isinstance` 放行的形状。
    """

    class Derived(ToolResultAgentMessage):
        extra: str = "父类没有的字段"

    with pytest.raises(UnknownMessageType):
        message_to_dict(Derived(timestamp=1, tool_call_id="c", tool_name="t", content=[]))


def test_jsonl_roundtrip_and_blank_lines() -> None:
    """JSONL 往返保住顺序与数量；空行被跳过而**非空行不得被跳过**。

    "跳过空行"是格式要求（追加写会在末尾留换行），
    但它与"跳过坏行"只差一行代码——所以要在这条里同时断言
    "空行不产生消息"与"三条非空行产出三条消息"。
    """
    messages: list[AgentMessage] = [
        LlmMessageWrapper(timestamp=1, message=UserMessage(content="你好", timestamp=1)),
        _tool_result_agent_message(),
        LlmMessageWrapper(
            timestamp=3,
            message=ToolResultMessage(
                tool_call_id="call-9",
                tool_name="bash",
                content=[TextBlock(text="ok")],
                timestamp=3,
            ),
        ),
    ]

    text = messages_to_jsonl(messages)
    # 追加写在末尾留一个换行是常规形态，这里显式造出来
    restored = messages_from_jsonl(text + "\n")

    assert len(restored) == 3
    for before, after in zip(messages, restored, strict=True):
        assert after.model_dump() == before.model_dump()


def test_jsonl_each_line_is_independently_valid() -> None:
    """每行必须是**独立可解析的 JSON 对象**，不是整个文件的 JSON 数组。

    这条钉的是格式本身：JSONL 的价值在于"可以逐行追加、逐行读取"。
    若实现改成 `json.dumps(list_of_dicts)`，它仍然是合法 JSON、
    往返也能过——但追加写与流式读取全部失效，
    且症状（长会话落盘变慢 / 内存占用）不指向格式。
    """
    text = messages_to_jsonl(
        [
            LlmMessageWrapper(timestamp=1, message=UserMessage(content="a", timestamp=1)),
            LlmMessageWrapper(timestamp=2, message=UserMessage(content="b", timestamp=2)),
        ]
    )

    lines = text.splitlines()
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert isinstance(parsed, dict)
        assert parsed["role"] == "llm"


# ---------------------------------------------------------------------------
# G18：架构边界——`sigma_ai` 不得依赖 `sigma_agent`
# ---------------------------------------------------------------------------


def _lint_imports_exe() -> str:
    """定位 `lint-imports`（与 `test_architecture_contracts.py` 一致的找法）。"""
    suffix = ".exe" if os.name == "nt" else ""
    beside = Path(sys.executable).parent / f"lint-imports{suffix}"
    if beside.exists():
        return str(beside)

    found = shutil.which("lint-imports")
    if found is None:
        raise RuntimeError("找不到 lint-imports。请先执行：pip install -e '.[dev]'")
    return found


def _run_lint_imports() -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [_lint_imports_exe()],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_layers_contract_rejects_ai_importing_agent() -> None:
    """G18 本体：往 `sigma_ai` 注入 `import sigma_agent`，契约必须变红。

    **这条是 E18 的真实形态**，而且它是**常驻的**——
    每次跑测试都会重新证伪一次，不依赖有人记得去手动执行注入脚本。

    **为什么值得这么写**：详规第 1 节已经实测过一次，
    但"实测过一次"是**瞬时证据**：契约配置、包名、
    `root_packages` 列表中的任何一项被改动，那次证据就作废了，
    而没人会收到通知。做成测试才是有持续性的约束。

    **注入点在 `messages.py`**：它是真正的"协议对齐层"里最不可能
    引入 agent 概念的文件——正因如此，它上面的违规最像"顺手 import"。
    **还原放在 `finally` 里**：注入中途抛错却留下破坏，
    比不做实验更糟（批次 1 固化的纪律）。
    """
    target = REPO_ROOT / "core" / "sigma_ai" / "messages.py"
    original = target.read_text(encoding="utf-8")

    injection = (
        "\n\n# --- 注入（G18 证伪用）：下层引用上层 ---\n"
        "from sigma_agent.agent_messages import AgentMessage  # noqa: F401\n"
    )

    try:
        target.write_text(original + injection, encoding="utf-8")

        result = _run_lint_imports()
        output = f"{result.stdout}\n{result.stderr}"

        assert result.returncode != 0, (
            "往 sigma_ai 注入 import sigma_agent 后契约仍然全绿——"
            f"分层契约没有覆盖这个方向：\n{output}"
        )
        assert "分层只允许向下依赖 BROKEN" in output, (
            f"变红但不是目标契约坏掉：\n{output}"
        )
    finally:
        target.write_text(original, encoding="utf-8")


def test_layers_contract_is_clean_after_restore() -> None:
    """G18 的收尾：还原之后必须**真的恢复全绿**。

    没有这条的话，"注入后变红"的结论是可疑的：
    契约可能因为**别的原因**变红（配置写坏、缓存污染），
    而本文件恰好在同一个进程里看不到区别。

    这条与上一条配对，构成一次完整的"红 → 绿"往返。
    """
    result = _run_lint_imports()
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, f"还原后契约没有恢复：\n{output}"

    match = re.search(r"Contracts: (\d+) kept, (\d+) broken", output)
    assert match is not None, f"无法解析契约统计：\n{output}"
    assert int(match.group(2)) == 0
