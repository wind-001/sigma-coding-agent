"""Provider 抽象层：抽象基类。

分层位置
    本模块属于 ``sigma_ai``，是依赖图的最底层——**不得引用任何其他 sigma 包**。
    由 import-linter 契约强制（架构方案第 8 节 / 门槛 G8）。

两段式
    数据载体用 Pydantic ``BaseModel``（``AGENTS.md`` 第 3 条，见 ``messages.py``）；
    抽象接口用 ``abc.ABC``（``AGENTS.md`` 第 2 条）。

    为什么用 ABC 而不是 ``typing.Protocol``：
    Protocol 是结构化类型，不产生继承关系，忘实现抽象方法时只在**首次调用**
    才暴露；ABC 在**实例化**就抛 ``TypeError``。「后期扩展」这个诉求下，
    早暴露远比晚暴露重要。

签名为什么有这么多参数
    ``sampling`` / ``options`` / ``timeout_s`` 三个参数是 2026-09-20 后补的
    （批次 1 详规 B1.3）。原先的签名只被 ``FakeProvider``（回放式实现）适配过——
    回放不需要采样、不需要请求用量、不会超时，于是签名看起来「刚好够用」。

    **真实协议一接进来，这三个参数一个都躲不掉。**

    经验的通用形式：**一个抽象基类的签名不能只被一个实现者适配，
    尤其当那个实现者是无感的回放器时。**（门槛 G10）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sigma_ai.events import StreamEvent
    from sigma_ai.messages import LlmMessage


# ---------------------------------------------------------------------------
# 请求参数（数据载体，BaseModel）
# ---------------------------------------------------------------------------


class SamplingParams(BaseModel):
    """采样参数。

    评测要控制变量，所以这几个参数必须能从上层传下来。
    """

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


class StreamOptions(BaseModel):
    """流式请求的可选项。

    ``include_usage`` 尤其关键：OpenAI 兼容协议**默认不在流里返回 usage**，
    必须显式请求。没有这个开关，用量统计只能靠估算——
    而估算数不能进评测报告（详规 4.1 节）。
    """

    include_usage: bool = True
    response_format: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 抽象基类（abc.ABC）
# ---------------------------------------------------------------------------


class CancelToken(ABC):
    """取消信号。

    用 ABC 是因为未来会有多种实现（signal / asyncio / 计时器）。
    抽象方法一律不提供默认实现——默认实现会让「忘了实现」变成静默错误。

    **注意**：这是自造类型，真实 ``httpx`` 场景用 ``asyncio.CancelledError``
    或 ``httpx.TimeoutException``。这个抽象**在批次 1 无法被完全验证**，
    需在批次 4 接真实 API 时复核（详规第 9 节 R1）。
    """

    @abstractmethod
    def is_cancelled(self) -> bool:
        """是否已请求取消。应当无副作用、可重复调用。"""
        raise NotImplementedError

    @abstractmethod
    def raise_if_cancelled(self) -> None:
        """若已取消则抛出。用于在循环体内主动检查。"""
        raise NotImplementedError


class BaseProvider(ABC):
    """所有 Provider 的抽象基类。

    子类必须实现 ``stream`` 与 ``estimate_tokens``。
    抽象方法一律不得提供默认实现——默认实现会让「忘了实现」变成静默错误。

    参数类型是 ``LlmMessage``，**不是** ``AgentMessage``：
    provider 层永远不认识 agent 层的消息，这条由 import-linter 契约强制（G8）。
    """

    @abstractmethod
    def stream(
        self,
        messages: list[LlmMessage],
        tools: list[dict[str, Any]],
        *,
        model: str,
        signal: CancelToken,
        sampling: SamplingParams | None = None,
        options: StreamOptions | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式产出统一事件。子类不得自行定义事件类型。

        三个关键字参数（``sampling`` / ``options`` / ``timeout_s``）是为了
        让签名同时被「回放式」与「协议式」两个实现者满足而补的，
        见模块 docstring。
        """
        raise NotImplementedError

    @abstractmethod
    def estimate_tokens(self, messages: list[LlmMessage]) -> int:
        """保守估算 token 数。

        **只用于预算预警，不作为账本。** 真实用量以 provider 返回的
        ``Usage`` 为准；本地估算要持续用真实值做比例校准。
        自实现 tokenizer 当唯一依据是错的。
        """
        raise NotImplementedError
