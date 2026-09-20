"""内存版会话上下文。

P1 只做**内存版**：不落盘、不压缩、不做会话树（后两者属 P2，落盘按本人要求跳过）。

它服务于 ``sdk`` 与 ``cli``，**loop 本身不依赖它**——
loop 参照 Pi 的形状，接收消息列表、产出消息（详规 3.8.1）。
所以这里的职责只有两件：

1. 累积历史。这本是调用方的事，但集中在一处总比散落在每个入口好；
2. **强制常驻区纪律**——见下。

常驻区指纹为什么 P1 就要有
    架构方案 5.2 节的判断：把它做成断言的成本是零，收益是把一条容易违反的约定
    变成不可违反的约束。而它是"prompt cache 命中率"这个指标的前提。

    **P1 的启动路径上确实没有东西会改常驻区**，所以这个断言在 P1 不会真的触发。
    也正因如此，它必须配一个**能证明它会失败**的测试（门槛 G26 + 注入 E24）：
    **不会失败的断言会被后来者当成死代码删掉。**

    一个"永远为真"的断言不是保守，是负债——它占着位置却不产生信息。
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from sigma_agent.agent_messages import LlmMessageWrapper
from sigma_ai.messages import SystemMessage

if TYPE_CHECKING:
    from collections.abc import Callable

    from sigma_agent.agent_messages import AgentMessage


class ResidentRegionChanged(RuntimeError):
    """常驻区在会话内被改动。

    **这是一个必须在会话开始前就修掉的错误，不是一个可以恢复的状态。**
    常驻区变了，prompt cache 从变动点起全部失效——
    而失效是静默的（只是变慢变贵，不会报错），所以必须在这里崩掉。
    """


class SessionContext:
    """内存版上下文：系统提示词 + 工具 schema（常驻区）+ 线性历史。

    常驻区（``system_prompt`` 与 ``tools_schema``）在构造时冻结指纹，
    之后每次组装消息都校验一次。
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        tools_schema: list[dict[str, Any]],
        clock: Callable[[], int],
        session_id: str = "sigma-session",
    ) -> None:
        self._system_prompt = system_prompt
        self._tools_schema = tools_schema
        self._clock = clock
        self._session_id = session_id
        self._history: list[AgentMessage] = []
        # 构造时冻结——之后每次组装都比对
        self._fingerprint = self._compute_fingerprint()

    # ------------------------------------------------------------------
    # 常驻区
    # ------------------------------------------------------------------

    def _compute_fingerprint(self) -> str:
        """常驻区的指纹。

        ``sort_keys=True`` 是必要的：``tools_schema`` 是嵌套 dict，
        键序不稳定会让同一个内容的指纹每次不同，于是**断言天天误报**——
        而误报的断言会被关掉，比没有更糟。
        """
        payload = json.dumps(
            {"system": self._system_prompt, "tools": self._tools_schema},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verify_resident_region(self) -> None:
        """校验常驻区未变。不一致就抛——**不修、不忍、不warning**。"""
        current = self._compute_fingerprint()
        if current != self._fingerprint:
            raise ResidentRegionChanged(
                "常驻区在会话内被改动了。这是 prompt cache 的充要条件（D4 / 架构 5.2 节），"
                "所以它必须崩而不是警告——失效是静默的（只是变慢变贵，不会报错）。\n"
                f"  冻结时指纹 {self._fingerprint}\n"
                f"  当前指纹   {current}\n"
                "常见原因：把时间戳/随机 ID 写进了 system 消息，"
                "或按任务动态拼了系统提示词。"
            )

    @property
    def fingerprint(self) -> str:
        """当前常驻区指纹（测试与审计用）。"""
        return self._compute_fingerprint()

    # ------------------------------------------------------------------
    # 历史
    # ------------------------------------------------------------------

    def append(self, *messages: AgentMessage) -> None:
        """追加消息。**只追加，不插入**（架构 5.2 节规则二）。

        插入会让 prompt cache 从插入点起全部失效。
        如果确实需要把内容放到前面，正确做法是压缩（P2），不是 insert。
        """
        self._history.extend(messages)

    def history(self) -> list[AgentMessage]:
        return list(self._history)

    def build_messages(self) -> list[AgentMessage]:
        """组装交给 loop 的完整消息列表。

        每次调用都**先校验常驻区**——这是这条纪律唯一的执行点。
        顺序：系统消息在前，历史在后。
        """
        self.verify_resident_region()
        system: AgentMessage = LlmMessageWrapper(
            timestamp=self._clock(),
            message=SystemMessage(content=self._system_prompt, timestamp=self._clock()),
        )
        return [system, *self._history]
