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

    参考数据：提示词 + 两个工具 schema 在真实 API 上实测约 875 token
    （2026-09-20，`scripts/real_api_agent_demo.py`）。现在工具是五个，
    该数字已过期——prompt token 以评测运行时的实测为准，旧值只作量级参考。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sigma_agent.agent_messages import AgentMessage, LlmMessageWrapper
from sigma_agent.loop import AgentLoop
from sigma_agent.observe import LoopObserver
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import TurnResult
from sigma_ai.base import NeverCancelled, SamplingParams
from sigma_ai.messages import UserMessage
from sigma_session.context import SessionContext
from sigma_tools.bash import BashTool
from sigma_tools.edit import EditTool
from sigma_tools.grep import GrepTool
from sigma_tools.read import ReadTool
from sigma_tools.write import WriteTool

if TYPE_CHECKING:
    from sigma_ai.base import BaseProvider


SYSTEM_PROMPT = """你是一个在本地工作区里干活的编程助手。

可用工具：
- read：读取文本文件，支持行范围（start_line / end_line）
- write：写入文件，覆盖原内容（新建文件或整体重写时用）
- edit：精确替换文件中的一段文本（修改已有文件时优先用它）
- bash：执行 bash 命令（列目录、建目录、运行测试等）
- grep：按正则搜索文件内容，返回 文件:行号:文本

工作方式：
1. 先看清楚再动手——不确定文件内容时先 read，不要凭猜测写。
2. 一次只做一件必要的事，不要把多步操作合成一次调用。
3. 完成后用一两句话说明你做了什么。

注意：
- 相对路径基于工作区根目录解析。
- write 不会自动创建父目录；建目录请用 bash 的 mkdir -p。
- bash 的命令没有任何过滤，执行前确认它符合当前任务。
"""


def default_registry() -> ToolRegistry:
    """P1 的内置工具集：read / write / edit / bash / grep 全部就位。

    bash 的风险要说清楚（详规 R1）：它以当前用户权限执行任意命令，
    P1 没有任何过滤与沙箱，防线只有 CLI 启动提示与评测的临时目录。
    """
    registry = ToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(BashTool())
    registry.register(GrepTool())
    return registry


def _real_clock() -> int:
    """当前时间戳。

    用 ``def`` 而不是 ``lambda``：**mypy strict 下 lambda 无法标注类型**，
    于是每次 ``clock()`` 调用都会报 ``no-untyped-call``。
    这是个容易忽略的约束——写 lambda 时不会想到它会被"调用类型检查"追上。
    """
    return int(time.time())


class InteractiveSession:
    """交互式会话：**跨轮复用同一个 ``SessionContext``**（门槛 G35）。

    为什么要有这个类，而不是让 CLI 自己拼
        ``-p`` 一次性模式与交互模式需要**同一套组装**（provider + 注册表 +
        上下文 + loop）。各写一遍的结果是两份会各自漂移的初始化代码，
        症状是"一次性模式跑通了但交互模式跑不通"——排查方向完全错。

    它**不做**什么
        不做中途打断 / 消息注入（P3 的 steering）。一条任务跑完整轮
        才能输入下一条，这是 P1 的明确边界，启动横幅里会写出来。
    """

    def __init__(
        self,
        *,
        provider: BaseProvider,
        workspace_root: Path,
        model: str,
        registry: ToolRegistry | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        max_rounds: int = 20,
        temperature: float = 0.0,
        observer: LoopObserver | None = None,
        emit: Callable[[str], None] | None = None,
        session_id: str = "sigma-session",
    ) -> None:
        self._registry = registry if registry is not None else default_registry()
        self._clock = _real_clock
        self._context = SessionContext(
            system_prompt=system_prompt,
            tools_schema=self._registry.schemas(),
            clock=self._clock,
            session_id=session_id,
        )
        self._loop = AgentLoop(
            provider=provider,
            registry=self._registry,
            model=model,
            session_id=session_id,
            workspace_root=workspace_root,
            max_rounds=max_rounds,
            sampling=SamplingParams(temperature=temperature),
            signal=NeverCancelled(),
            clock=self._clock,
            emit=emit,
            observer=observer,
        )

    async def send(self, task: str) -> TurnResult:
        """发一条任务，跑完整轮，把产出追加回历史。

        追加这一步**必须在这里**——loop 参照 Pi 的形状不持有会话对象
        （详规 3.8.1），历史归调用方管。
        """
        now = self._clock()
        self._context.append(
            LlmMessageWrapper(
                timestamp=now, message=UserMessage(content=task, timestamp=now)
            )
        )
        result = await self._loop.run_turn(self._context.build_messages())
        self._context.append(*result.messages)
        return result

    def history(self) -> list[AgentMessage]:
        """历史消息（副本）。**跨轮累积**是这个类存在的意义（门槛 G35）。

        返回副本而不是内部列表：调用方拿去改不会污染会话状态。
        """
        return self._context.history()


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
    observer: LoopObserver | None = None,
    session_id: str = "sigma-session",
) -> TurnResult:
    """跑一个任务，返回结果。**一次性会话**（发一条、跑完、结束）。

    ``workspace_root`` 决定工具里相对路径的基准——**它同时是 CLI 的 ``--workspace``**。
    注意它**不是安全边界**（D5 的三层软边界在 P1 全未落地，见详规 0.1 代价第 3 条）。

    时间戳用真实时钟。若要让执行**确定**（回放测试要求两次逐字节一致），
    调用方应自行构造 ``AgentLoop`` 并注入固定 ``clock`` ——
    ``run_task`` 面向"真跑"，不为可复现性妥协。

    它复用 :class:`InteractiveSession` 的组装——**只有一处组装，不会漂移**。
    """
    session = InteractiveSession(
        provider=provider,
        workspace_root=workspace_root,
        model=model,
        registry=registry,
        system_prompt=system_prompt,
        max_rounds=max_rounds,
        temperature=temperature,
        observer=observer,
        emit=emit,
        session_id=session_id,
    )
    return await session.send(task)
