"""LLM 层消息 → Anthropic Messages 请求体的转换。

为什么单独成文件
    与 ``openai/convert.py`` 同一条判据：转换是本层**最容易出错**的地方，
    值得一个能被单测直接打的落点。本模块是纯函数，不碰网络、不持有状态。

与 OpenAI 的四个结构差异（详规 §3 的表，这里逐条落地）

    1. **system 是顶层参数**，不是 message——``SystemMessage`` 提出来放进
       转换结果的 ``system`` 字段；P1 设计保证只会有一条（架构 4.0.4），
       多条即 ``ValueError``，宁可崩不要错。
    2. **tool_result 是 user 消息里的 content block**，不是独立 role——
       ``ToolResultMessage`` 转成 ``{"role": "user", "content": [tool_result 块]}``，
       连续多条合并进**同一条** user 消息（Anthropic 要求并行调用的结果
       放在一条 user 消息里，拆开发会被拒收）。
    3. **assistant 的工具调用在 content 里**（``tool_use`` 块），且 arguments
       是**对象**不是 JSON 字符串——``json.dumps`` 在这一侧是接错。
    4. 工具 schema 字段名不同——``function.parameters`` → ``input_schema``
       直挂（``ToolRegistry.schemas()`` 的产物是 OpenAI 形状，转这里）。

交替约束
    Anthropic 要求消息序列严格 user/assistant 交替。sigma 的树形历史天然
    满足；转换器仍要检查——违反即 ``ValueError``（列出 role 序列），
    让坏历史在**发请求之前**崩，而不是等 API 400 之后再猜。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sigma.providers.messages import (
    AssistantMessage,
    ImageBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from sigma.providers.messages import ContentBlock, LlmMessage


class DroppedThinkingBlock(UserWarning):
    """v1 把 thinking 块从请求里丢弃时发出（详规 §3：防御性丢弃必须留痕）。"""


@dataclass(frozen=True)
class AnthropicConversion:
    """一次消息转换的产物。

    **为什么是 dataclass 而不是 BaseModel**：它是转换函数的进程内返回值，
    被 provider 立刻拼进请求体——不落盘、不过网（判据与
    ``StreamOptions`` / ``TruncatedText`` 一致）。

    ``dropped_thinking_blocks`` 单列成字段是"**丢弃必须可见**"的落地：
    v1 不主动开启 extended thinking，历史上出现 thinking 块只可能是
    防御路径——丢弃的数量在返回值里看得见，不靠调用方自己数。
    """

    system: str | list[dict[str, Any]] | None
    messages: list[dict[str, Any]]
    dropped_thinking_blocks: int = 0


# ---------------------------------------------------------------------------
# 内容块转换
# ---------------------------------------------------------------------------


def _system_content(message: SystemMessage) -> str | list[dict[str, Any]]:
    """system 消息的内容 → 顶层 ``system`` 参数（字符串或块数组）。

    单个文本块降级成裸字符串——少一层嵌套、少几个 token。
    """
    if isinstance(message.content, str):
        return message.content
    if len(message.content) == 1:
        return message.content[0].text
    return [{"type": "text", "text": block.text} for block in message.content]


def _user_blocks(content: str | list[ContentBlock]) -> str | list[dict[str, Any]]:
    """user 消息的内容 → Anthropic 的 ``content`` 形状。

    纯文本降级成裸字符串。``ImageBlock`` 转 Anthropic 的 base64 图片块：
    v1 **不主动发**图片（详规 §1 的范围红线），但消息里已有图片块时
    转换是忠实透传，丢弃反而是静默丢信息。认不出的块类型抛
    ``TypeError``——不静默丢弃（架构 4.0 纪律）。
    """
    if isinstance(content, str):
        return content

    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.mime_type,
                        "data": block.data,
                    },
                }
            )
        else:
            raise TypeError(
                f"user 消息里无法转换的内容块类型：{type(block).__name__}"
            )
    if len(parts) == 1 and parts[0].get("type") == "text":
        return str(parts[0]["text"])
    return parts


def _tool_result_block(message: ToolResultMessage) -> dict[str, Any]:
    """工具结果 → ``tool_result`` content block。

    ``content`` 按详规固定为**块数组**。多模态内容块不支持且**必须报错**
    而不是静默降级成 ``str(list)``——那会产生没人能读的字符串
    （与 openai/convert.py 同一条纪律）。空结果发空串：Anthropic 接受，
    且"没有输出"本身就是需要模型看到的事实。
    """
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            blocks.append({"type": "text", "text": block.text})
        else:
            raise TypeError("工具结果的 content 暂不支持多模态内容块")
    return {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id,
        "content": blocks or "",
        "is_error": message.is_error,
    }


def _assistant_blocks(
    content: list[ContentBlock], dropped: list[int]
) -> list[dict[str, Any]]:
    """assistant 消息的内容 → ``text`` / ``tool_use`` 块数组。

    ``ThinkingBlock`` 为什么是丢弃而不是报错
        v1 不开启 extended thinking，正常路径不会出现 thinking 块；
        万一出现（历史里带过来的防御场景），Anthropic 在未开启 thinking
        时**拒收** thinking 块，所以只能不发。但丢弃必须留痕：计数进
        ``dropped_thinking_blocks`` 并发 ``UserWarning``——与"丢弃必须可见"
        纪律同源。输入消息本身不动，``thinking_signature`` 原样留在本地。

    ``dropped`` 为什么是可变列表
        递归函数里传计数器比传返回值再拼回来少一层易错的手工合并。
    """
    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolCallBlock):
            # **input 是对象不是 JSON 字符串**——Anthropic 与 OpenAI 在这
            # 一点上方向相反，从 openai 侧抄形状会直接被 API 拒收。
            parts.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.arguments,
                }
            )
        elif isinstance(block, ThinkingBlock):
            dropped[0] += 1
            warnings.warn(
                f"v1 丢弃 assistant 消息里的 thinking 块（{len(block.thinking)} 字符，"
                "签名保留在本地消息中）——未开启 extended thinking 时 Anthropic 拒收该块",
                DroppedThinkingBlock,
                stacklevel=4,
            )
            continue
        else:
            raise TypeError(
                f"assistant 消息里无法转换的内容块类型：{type(block).__name__}"
            )
    return parts


# ---------------------------------------------------------------------------
# 消息序列转换
# ---------------------------------------------------------------------------


def messages_to_anthropic(messages: list[LlmMessage]) -> AnthropicConversion:
    """把整条 LLM 消息序列转成 Anthropic 请求体的 ``system`` + ``messages``。

    必须收**整条序列**而不是单条消息：system 顶层化与 tool_result 合并
    都是**序列级**的决定，单条转换表达不了"连续的 tool_result 归并成
    一条 user 消息"。

    三条硬校验（全部**宁可崩不要错**）：

    - system 多于一条 → ``ValueError``（架构 4.0.4 保证只有一条）；
    - 丢弃 thinking 后 content 为空 → ``ValueError``（Anthropic 拒收空 content，
      发出去只会换来一个更难懂的 400）；
    - user/assistant 不交替 → ``ValueError``（列出 role 序列）。
    """
    system: str | list[dict[str, Any]] | None = None
    converted: list[dict[str, Any]] = []
    dropped_count = 0
    # 待合并的 tool_result 宿主：只有**连续的** ToolResultMessage 会并进去，
    # 任何其他消息到达都断开合并链（断开后若再出现 user 形态，交替检查兜住）。
    pending_tool_result_user: dict[str, Any] | None = None

    for message in messages:
        if isinstance(message, SystemMessage):
            if system is not None:
                raise ValueError(
                    "system 消息最多允许一条（架构 4.0.4），"
                    f"实际遇到第二条：{message.content!r}"
                )
            system = _system_content(message)
            pending_tool_result_user = None
            continue

        if isinstance(message, UserMessage):
            converted.append({"role": "user", "content": _user_blocks(message.content)})
            pending_tool_result_user = None
            continue

        if isinstance(message, AssistantMessage):
            dropped: list[int] = [0]
            blocks = _assistant_blocks(message.content, dropped)
            dropped_count += dropped[0]
            if not blocks:
                raise ValueError(
                    "assistant 消息在丢弃 thinking 块后 content 为空，"
                    "Anthropic 拒收空 content——请在调用方补可见注记"
                )
            converted.append({"role": "assistant", "content": blocks})
            pending_tool_result_user = None
            continue

        if isinstance(message, ToolResultMessage):
            block = _tool_result_block(message)
            if pending_tool_result_user is not None:
                pending_tool_result_user["content"].append(block)
            else:
                pending_tool_result_user = {"role": "user", "content": [block]}
                converted.append(pending_tool_result_user)
            continue

        raise ValueError(f"未知的 LLM 消息类型：{type(message).__name__}")

    roles = [entry["role"] for entry in converted]
    for previous, current in zip(roles, roles[1:]):
        if previous == current:
            raise ValueError(
                f"消息序列必须严格 user/assistant 交替，实际 role 序列：{roles}"
            )

    return AnthropicConversion(
        system=system,
        messages=converted,
        dropped_thinking_blocks=dropped_count,
    )


# ---------------------------------------------------------------------------
# 工具定义转换
# ---------------------------------------------------------------------------


def tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具定义 OpenAI 形状 → Anthropic 形状。

    上游（``ToolRegistry.schemas()``）给的是 OpenAI 形状
    ``{"type": "function", "function": {"name", "description", "parameters"}}``；
    Anthropic 要的是 ``{"name", "description", "input_schema"}``——
    ``parameters`` **直挂**成 ``input_schema``（本来就是 ``model_json_schema()``
    的产物，不需要再加工）。``cache_control`` v1 不加（详规 §1 范围红线）。

    认不出的形状抛 ``ValueError``——工具静默消失的症状是"模型永远不调工具"，
    完全不指向根因。
    """
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            raise ValueError(f"无法转换的工具定义形状（缺 function.name）：{tool!r}")
        anthropic_tool: dict[str, Any] = {
            "name": function["name"],
            # input_schema 是 Anthropic 的必填字段；上游没给时兜一个空对象 schema，
            # 让"无参工具"仍可注册，而不是被 API 以缺字段拒收。
            "input_schema": function.get("parameters")
            or {"type": "object", "properties": {}},
        }
        if function.get("description"):
            anthropic_tool["description"] = function["description"]
        converted.append(anthropic_tool)
    return converted
