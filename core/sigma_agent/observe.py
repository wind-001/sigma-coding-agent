"""loop 的观测接口：**单一 ``on_event`` + 事件对象**。

为什么是"一个方法"而不是每种事件一个方法（W9）
    每加一个观测点就要给所有观察者加一个方法；只想看工具调用的实现者
    也得写一堆空方法。而事件是**数据**：新增观测点 = 新增一个 dataclass，
    **接口不变**。P3 做 steering 时可以直接复用这条通道。

为什么不是把 ``run_turn`` 改成 async generator（W9 丙）
    那会改 loop 的对外契约（``run_turn -> TurnResult``），而 P2 接会话树
    正是依赖这个形状。为了渲染去动核心契约不划算——观测是**旁听**，不是主流程。

为什么事件用 dataclass 而不是 BaseModel
    文本增量**每个 chunk 一个事件**，是最高频的对象；它们不落盘、不需要校验。
    判据与 ``TurnResult`` 一致——**基类答"谁是谁"，模型答"装着什么"**，
    而事件两样都不是，它只是一条瞬时通知。

为什么 ``on_event`` 有默认空实现、而不是抽象方法
    判据与 ``base.py`` 一致：**空实现对所有观察者都正确**；
    逼每个观察者实现它，只会让"只想看工具调用"的人写一堆空方法。
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TextChunk:
    """模型产出的文本增量。**逐块透传，不聚合**——聚合了就没有"流式"了。"""

    text: str


@dataclass(frozen=True)
class ThinkingChunk:
    """思考增量。P1 的 OpenAI 兼容 provider 不产生它，类型先留着不给后来人挖坑。"""

    text: str


@dataclass(frozen=True)
class ToolStart:
    """一次工具调用**即将执行**。

    在执行**之前**发而不是之后：模型"决定调什么"本身就是过程的一部分，
    而 bash 最长 60 s，执行期间终端一片空白会让人以为卡死。
    """

    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class ToolEnd:
    """一次工具调用结束。``ok`` 是 ``not result.is_error``。"""

    name: str
    ok: bool
    preview: str


@dataclass(frozen=True)
class TurnEnd:
    """一次 ``run_turn`` 结束。带 usage 是为了让终端能提示上下文压力（风险 R1）。"""

    status: str
    rounds: int
    prompt_tokens: int
    completion_tokens: int


LoopEvent = TextChunk | ThinkingChunk | ToolStart | ToolEnd | TurnEnd
"""观测事件的判别联合。"""


class LoopObserver(ABC):
    """loop 的观察者。终端渲染、落盘、测试 recorder 都实现它。"""

    def on_event(self, event: LoopEvent) -> None:
        """收到一个事件。**默认什么也不做**。

        子类要么覆写它，要么就保持沉默——这正是"空实现对所有观察者都正确"的意思。
        """
