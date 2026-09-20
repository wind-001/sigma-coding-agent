"""LLM 层消息模型：四个消息 + 四个内容块。

分层位置
    本模块属于 **LLM 层**（``docs/architecture.md`` 4.0.1 / 4.0.2 节）。
    它严格对应 wire protocol，因此**不做抽象、没有基类**——
    加基类只会让协议转换更难写，且没有行为需要统一分派。
    需要抽象的是 agent 层（4.0.3 节），那里有 ``to_llm()`` 这个行为。

为什么 ``role`` 是四个值
    ``tool_result`` 是**独立角色**，不是塞在 assistant 消息里的内容块。
    这与 OpenAI 兼容协议一致（工具结果有自己的 ``tool_call_id``），
    转换时少一层映射。见 4.0.1 节第 1 条。

三个签名类字段为什么 P1 就要有
    ``text_signature`` / ``thinking_signature`` / ``thought_signature``
    是 provider 返回的、**必须原样回传的不透明串**。
    P1 用不到，但漏了会让多轮对话行为异常，而且**症状完全不指向根因**。
    对应门槛 G2：JSONL 往返后逐字节不变。

本模块的依赖边界
    **只依赖 ``pydantic``**，不 import 任何其他 sigma 模块。
    ``Usage`` 与 ``StopReason`` 在此就地定义，理由见下方注释。

来源
    ``docs/pi-harness研究笔记.md`` 第 9.5.2 / 9.5.3 节（Pi 0.86.0 一手源码）
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 基础数据载体
# ---------------------------------------------------------------------------


class Usage(BaseModel):
    """用量统计。

    ``cached_tokens`` 独立成字段，因为 prompt cache 命中率是 D4 节的核心指标——
    它得能单独看出来，不能混在 ``prompt_tokens`` 里。
    """

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0


StopReason = Literal[
    "stop",  # 正常结束
    "length",  # 达到 max_tokens
    "tool_use",  # 模型要调用工具
    "content_filter",  # 被安全过滤器中断
    "error",  # 出错
]
"""停止原因。

用 ``Literal`` 而非 ``StrEnum``：它直接对应协议里的字符串取值，
不需要做一次映射。缺点是没有运行期穷尽性检查——由单测补。
"""


class ToolMeta(BaseModel):
    """工具的轻量元数据。

    只用于 ``SystemMessage.tools_added``——按名移除工具时用不着完整定义
    （对应 Pi 的 ``ToolReference``，见调研笔记 9.5.8）。
    完整的工具定义在 ``sigma_agent`` 层，**本层不认识它**。
    """

    name: str
    description: str = ""
    read_only: bool = False


# ---------------------------------------------------------------------------
# 内容块：四个模型，靠 ``type`` 判别（架构方案 4.0.2）
# ---------------------------------------------------------------------------


class TextBlock(BaseModel):
    """文本内容块。"""

    type: Literal["text"] = "text"
    text: str
    # provider 返回的不透明串，必须原样回传。见模块 docstring。
    text_signature: str | None = None


class ThinkingBlock(BaseModel):
    """思考内容块（兼容 Anthropic 的 thinking）。"""

    type: Literal["thinking"] = "thinking"
    thinking: str
    # 被安全过滤器遮蔽时，不透明加密载荷存在这里，必须回传才能维持多轮连贯
    thinking_signature: str | None = None
    redacted: bool = False


class ImageBlock(BaseModel):
    """图片内容块。"""

    type: Literal["image"] = "image"
    data: str  # base64
    mime_type: str


class ToolCallBlock(BaseModel):
    """工具调用块。只描述"要调用什么"，不含执行结果。"""

    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]
    # provider 返回的不透明串（如 Gemini 的 thought signature）
    thought_signature: str | None = None


ContentBlock = TextBlock | ThinkingBlock | ImageBlock | ToolCallBlock


# ---------------------------------------------------------------------------
# 消息：四个具体模型，没有基类（架构方案 4.0.1）
# ---------------------------------------------------------------------------


class SystemMessage(BaseModel):
    """system 消息。

    ⚠️ **不要往这里放压缩摘要。**

    摘要必须降级成 :class:`UserMessage`（架构方案 4.0.6 节）。理由：system
    消息进常驻区，而常驻区在会话内**必须逐字节稳定**（D4）；中途变化会让
    prompt cache 从变动点起全部失效。

    **本类字段集合由单测断言固定**（门槛 G9）。新增字段会导致测试失败——
    这是有意的，用来挡住"顺手把摘要塞进 system 消息"。

    ``sections`` / ``tools_added`` / ``tools_removed`` 让 system 消息承担
    提示词演进与工具集变更的**记录**职责：按顺序重放所有 system 消息，
    就得到当前提示词与当前工具集（4.0.4 节）。

    **P1 只在会话开始处发一条 system 消息**：中途插入会破坏 prompt cache，
    该能力保留类型但不启用，等 P2 做压缩时一并设计缓存策略（4.3 节）。
    """

    role: Literal["system"] = "system"
    content: str | list[TextBlock]
    # 具名提示词段落的增删改；值为 None 表示删除该段
    sections: dict[str, str | None] | None = None
    tools_added: list[ToolMeta] | None = None
    tools_removed: list[str] | None = None  # 仅工具名，对应 Pi 的 ToolReference
    timestamp: int


class UserMessage(BaseModel):
    """user 消息。

    压缩摘要、分支摘要都降级成这个类型（而不是 system），
    这样它们不占常驻区、不破坏 prompt cache。
    """

    role: Literal["user"] = "user"
    content: str | list[ContentBlock]
    timestamp: int


class AssistantMessage(BaseModel):
    """assistant 消息。

    ``api`` / ``provider`` / ``model`` / ``response_id`` 四个字段是 provider
    的元数据，用于回放与审计——**不是**给模型看的。
    """

    role: Literal["assistant"] = "assistant"
    content: list[ContentBlock]
    api: str = ""
    provider: str = ""
    model: str = ""
    response_id: str = ""
    usage: Usage
    stop_reason: StopReason
    error_message: str = ""
    timestamp: int


class ToolResultMessage(BaseModel):
    """工具结果消息。**独立角色**，不是 assistant 的内容块。"""

    role: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    content: list[ContentBlock]
    # 不进上下文，只给 UI / 审计 / 评测。判据：不需要模型看到的内容都放这里。
    details: dict[str, Any] = {}
    is_error: bool = False
    timestamp: int


LlmMessage = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
