"""函数式入口：把各层组装起来，跑一个任务。

这是"能启动"的最小闭环（架构方案 9 节 P1 的交付项 14）。

它**不含业务逻辑，只做接线**。理由很实际：CLI、测试、评测运行器三处
都需要同一套组装（provider + 注册表 + 上下文 + loop），各写一遍的结果
是三份会各自漂移的初始化代码——而漂移出来的差异，
症状是"评测跑通了但 CLI 跑不通"，排查方向完全错。

系统提示词为什么是常量、且刻意写短
    D4 / 架构 5.2 节：常驻区要 ≤ 3,500 token 且**逐字节稳定**。
    所以：

    - 不按任务动态调整（那会让常驻区每轮都变，prompt cache 全失效）；
    - 不放时间戳、会话 ID（同上）；
    - 写短不是省 token，是**把预算留给历史与工具输出**。

    参考数据：真实 API 实测这个提示词 + 两个工具 schema 合计约 875 token
    （`scripts/real_api_agent_demo.py` 的 prompt_tokens）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sigma_agent.agent_messages import AgentMessage, LlmMessageWrapper
from sigma_agent.loop import AgentLoop
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import TurnResult
from sigma_ai.base import NeverCancelled, SamplingParams
from sigma_ai.messages import UserMessage
from sigma_session.context import SessionContext
from sigma_tools.read import ReadTool
from sigma_tools.write import WriteTool

if TYPE_CHECKING:
    from sigma_ai.base import BaseProvider


SYSTEM_PROMPT = """你是一个在本地工作区里干活的编程助手。

可用工具：
- read：读取文本文件，支持行范围（start_line / end_line）
- write：写入文件，覆盖原内容

工作方式：
1. 先看清楚再动手——不确定文件内容时先 read，不要凭猜测写。
2. 一次只做一件必要的事，不要把多步操作合成一次调用。
3. 完成后用一两句话说明你做了什么。

注意：
- 相对路径基于工作区根目录解析。
- write 不会自动创建父目录，父目录不存在会报错。
"""


def default_registry() -> ToolRegistry:
    """P1 的内置工具集。

    **目前只有 read / write**：``edit`` / ``bash`` / ``grep`` 尚未实现
    （计划见 `docs/plans/P1-批次2-4-详规.md` 3.5 节）。

    工具少不是省事，是有代价的：``edit`` 缺席意味着模型改文件只能整文件重写，
    那更容易出错也更费 token。**这件事要说出来，不能假装够用。**
    """
    registry = ToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    return registry


async def run_task(
    task: str,
    *,
    provider: BaseProvider,
    workspace_root: Path,
    model: str,
    max_rounds: int = 20,
    registry: ToolRegistry | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    temperature: float = 0.0,
    emit: Callable[[str], None] | None = None,
    session_id: str = "sigma-session",
) -> TurnResult:
    """跑一个任务，返回结果。

    ``workspace_root`` 决定工具里相对路径的基准——**它同时是 CLI 的 ``--workspace``**。
    注意它**不是安全边界**（D5 的三层软边界在 P1 全未落地，见详规 0.1 代价第 3 条）。

    时间戳用真实时钟。若要让执行**确定**（回放测试要求两次逐字节一致），
    调用方应自行构造 ``AgentLoop`` 并注入固定 ``clock`` ——
    ``run_task`` 面向"真跑"，不为可复现性妥协。
    """
    registry = registry if registry is not None else default_registry()

    def clock() -> int:
        """当前时间戳。

        用 ``def`` 而不是 ``lambda``：**mypy strict 下 lambda 无法标注类型**，
        于是每次 ``clock()`` 调用都会报 ``no-untyped-call``。
        这是个容易忽略的约束——写 lambda 时不会想到它会被"调用类型检查"追上。
        """
        return int(time.time())

    context = SessionContext(
        system_prompt=system_prompt,
        tools_schema=registry.schemas(),
        clock=clock,
        session_id=session_id,
    )
    context.append(
        LlmMessageWrapper(
            timestamp=clock(),
            message=UserMessage(content=task, timestamp=clock()),
        )
    )

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        model=model,
        session_id=session_id,
        workspace_root=workspace_root,
        max_rounds=max_rounds,
        sampling=SamplingParams(temperature=temperature),
        signal=NeverCancelled(),
        clock=clock,
        emit=emit,
    )

    # 组装与消费在同一步完成：``run_turn`` 接收历史、返回新增消息，
    # 追加回上下文由这里负责（loop 不持有会话对象，详规 3.8.1）。
    result = await loop.run_turn(context.build_messages())

    produced: list[AgentMessage] = list(result.messages)
    context.append(*produced)
    return result
