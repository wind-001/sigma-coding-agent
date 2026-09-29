"""事件机制：**纯事件定义,零行为**——hooks/ 是订阅者,本包是被订阅的东西。

与 providers/events.py(流事件)的分界(P6 拍板 Q2)
    流事件(``StreamEvent``)是 Provider 抽象的返回类型,属协议表面,留在
    providers;本文件是 harness 内部总线的**生命周期事件**。两套事件在
    runtime 汇合:loop 消费流事件、把生命周期事件 emit 给钩子。

三条设计判据(承自 P4-批次6 的钩子哲学,星辰拍板)

1. **钩子点是一个触发时机,由事件驱动**:事件类型本身 = 时机。
   新增钩子点 = 新增一个事件 dataclass + loop 里一处 ``emit``;
   "所有 hook 都要加一个方法"的那种接口不会出现。
2. **事件是瞬时通知,不是领域模型**:frozen dataclass,不落盘、不校验;
   真正落盘的 ``AgentMessage`` 在载荷里,那才是 BaseModel。
3. **事件不带时间**:配对与计时归订阅者(TraceHook),事件本身只是通知。

``ApprovalDecision`` 为何住在这里(P6 拍板偏差#2)
    它是 :class:`ApprovalDecided` 事件的载荷——事件与载荷同处一个文件;
    放 hooks/base 会让本模块对 hooks 产生前向引用(mypy 下成上翻)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sigma.agent.messages import AgentMessage


@dataclass(frozen=True)
class ApprovalDecision:
    """一次审批的**决定**（frozen dataclass,P6 批次C 三问判定:
    不生成 schema、不落盘、按键结果直接构造——三问皆否）。

    ``allowed=False`` 时 ``reason`` 必填——它会进工具结果,模型要能读到
    "为什么被拒"才能自我纠正(与"单个失败不中断批次"同构)。

    ``approve_outside``:本次调用需要**越出工作区**(write/edit 的目标路径
    在工作区之外)且审批方明示豁免 → loop 把它传进 ``ToolContext``,
    L1(``resolve_write_path``)据此放行。默认 False:L1 的拒绝语义不动。
    """

    allowed: bool
    reason: str = ""
    note: str = ""
    approve_outside: bool = False


@dataclass(frozen=True)
class AssistantProduced:
    """钩子点：**每一次 LLM 返回**。

    触发位置在错误判断**之前**——错误轮的 partial 内容也先经此事件
    落盘再被定性（"留下审计"与"如实报错"两件事都做，与现状口径一致）。

    ``message`` 是进入 produced 的那条 agent 层消息（``LlmMessageWrapper``
    包装的 ``AssistantMessage``）。事件带包装后的形态是因为**订阅方要的
    就是"将被追加进历史的那条消息"**——拆开再包回去会出现两份包装逻辑。
    """

    message: AgentMessage


@dataclass(frozen=True)
class MessageInjected:
    """钩子点：steering 提醒 / 信箱回报等**注入**进 produced 的消息。

    为什么单独一个点：信箱是 drain-once——子任务结果从 TaskTool 取走的
    那一刻起只存在于内存，不在此刻落盘，中断就真丢了。
    """

    message: AgentMessage


@dataclass(frozen=True)
class TextChunk:
    """钩子点：模型产出的文本**增量**。**逐块透传，不聚合**——聚合了就没有"流式"了。"""

    text: str


@dataclass(frozen=True)
class ThinkingChunk:
    """钩子点：思考增量。P1 的 OpenAI 兼容 provider 不产生它，类型先留着不给后来人挖坑。"""

    text: str


@dataclass(frozen=True)
class LlmRequested:
    """钩子点：**即将发起一次模型请求**（P5-批次1，观测）。

    为什么在 loop 发、而不是 provider 内部测：provider 层不认识钩子
    （import-linter 契约），且"换 provider 不改测量点"是回放口径的前提。
    与 :class:`AssistantProduced`（返回）及首个 :class:`TextChunk`（首 token）
    配对，派生单次调用延迟与 TTFT——**配对与计时都归订阅者**（TraceHook），
    事件本身不带时间：事件是瞬时通知，不是领域模型（判据 2）。
    """


@dataclass(frozen=True)
class ToolStart:
    """钩子点：一次工具调用**即将执行**。

    在执行**之前**发而不是之后：模型"决定调什么"本身就是过程的一部分，
    而 bash 最长 60 s，执行期间终端一片空白会让人以为卡死。
    保证：同一批次里所有 ``ToolStart`` 都早于任何 :class:`ToolEnd`
    （顺序错了，终端会"先出结果后出调用"）。
    """

    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class ToolEnd:
    """钩子点：**每一个工具结果落定**（含 unparsed 合成的失败结果）。

    由批次5 的 ``ToolResultProduced`` 与观测通道的 ``ToolEnd`` 合并而来
    （P4-批次6）：**持久化与渲染是同一个时机的两个订阅者**，不该发两次
    事件、更不该有两个名字。``message`` 给持久化（要落盘的那条消息），
    ``name/ok/preview`` 给渲染（一行可读摘要）。
    """

    name: str
    ok: bool
    preview: str
    message: AgentMessage


@dataclass(frozen=True)
class TurnEnd:
    """钩子点：一次 ``run_turn`` 结束。带 usage 是为了让终端提示上下文压力（R1）。"""

    status: str
    rounds: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class ApprovalDecided:
    """钩子点：一次审批询问的**结论**（P5-批次1，观测）。

    批准与拒绝都发——"allowlist 命中放行"与"命中危险模式被拒"同为
    需要追溯的观测事实；此前审批决策零留痕，会话 JSONL 里只能看到
    拒绝结果的间接影子（``details={"approval": "denied"}``），放行则完全不可见。

    ``decision`` 的类型 :class:`ApprovalDecision` 定义在本文件上方
    （事件与载荷同处,见模块 docstring）。
    **不带 arguments**：reason 已携带"命中了什么"，参数原样落盘是
    隐私与体积的双重负担（工具参数里可能有整段代码）。
    """

    name: str
    decision: ApprovalDecision


HookEvent = (
    AssistantProduced
    | MessageInjected
    | TextChunk
    | ThinkingChunk
    | LlmRequested
    | ToolStart
    | ToolEnd
    | TurnEnd
    | ApprovalDecided
)
"""全部钩子点。**新增时机 = 新增一个成员 + loop 里一处 emit**，接口不变。"""
