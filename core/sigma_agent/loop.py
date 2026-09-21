"""agent loop：唯一的循环实现。

形状参照 Pi 的 ``agentLoop``（调研笔记第 5 节）
    **消息数组进、新增消息出，循环本身不持有会话对象。**

    "存到哪儿"是调用方的事。这个形状带来三件事（详规 3.8.1）：

    1. P1 不需要会话管理——不必为了签名好看去造 ``SessionTree`` 空壳；
    2. loop 是无状态的，测试可以直接喂消息、断言输出，不必先搭会话设施；
    3. **P2 引入会话树时，只要在调用处把树上的消息取出来传进去，本文件一行不用改。**

一轮的八步（与 Pi 逐条对照见详规 3.8.1）
    接收消息 → [transformContext：P1 无钩子，跳过] → convertToLlm →
    流式取文本与工具调用 → **校验工具参数** → 执行完整批次 →
    追加工具结果 → 判断是否再来一轮

三处最容易写错、且都有专门门槛钉住的地方
    1. **工具分片按 ``index`` 归属**，不能按到达顺序硬拼（门槛 G27）；
    2. **参数校验失败要变成模型可见的错误结果**，不是异常（门槛 G31）；
    3. **loop 里不能有"第一个工具失败就 return"的分支**——
       它看起来像防御性编程，实际会静默关掉整个纠错能力（详规 3.6）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from sigma_agent.agent_messages import (
    AgentMessage,
    LlmMessageWrapper,
    ToolResultAgentMessage,
    convert_to_llm,
)
from sigma_agent.base import BaseTool
from sigma_agent.observe import (
    LoopEvent,
    LoopObserver,
    TextChunk,
    ThinkingChunk,
    ToolEnd,
    ToolStart,
    TurnEnd,
)
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import ToolContext, ToolResult, TurnResult
from sigma_ai.base import CancelToken, SamplingParams
from sigma_ai.events import (
    ErrorEvent,
    StopEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageEvent,
)
from sigma_ai.messages import (
    AssistantMessage,
    ContentBlock,
    LlmMessage,
    TextBlock,
    ToolCallBlock,
    Usage,
)
from sigma_ai.tool_calls import AssembledCall, ToolCallAssembler

if TYPE_CHECKING:
    from sigma_ai.base import BaseProvider


# `ParsedCall` 已于 2026-09-20 删除：它的字段与协议层的
# `sigma_ai.tool_calls.AssembledCall` 几乎完全一样，
# 而**同一个概念不该有两份定义**。
#
# 详规子项 C：拼装逻辑收进协议层（那是 wire protocol 的知识），
# agent 层只负责"这个调用该不该执行、失败了怎么办"。


@dataclass
class _Planned:
    """一个准备执行的调用：位置 + 调用 + 校验过的参数 + 工具实例。"""

    position: int
    call: ToolCallBlock
    args: Any
    tool: BaseTool


class AgentLoop:
    """agent 循环的**唯一实现**。

    2026-09-20 删除了它原来的基类 ``BaseLoop``——那是个只有 1 个子类的抽象，
    等价于给唯一实现加一层纯间接。删除理由见 ``base.py`` 顶部注释。

    门槛 G30 已改为断言「全项目只有一个类定义 ``run_turn``」，
    比原来的 ``__subclasses__()`` 断言**更强**：新写一个不继承任何基类的
    loop 也能拦住，而旧写法拦不住。
    """

    def __init__(
        self,
        *,
        provider: BaseProvider,
        registry: ToolRegistry,
        model: str,
        session_id: str = "sigma-session",
        workspace_root: Any = None,
        max_rounds: int = 20,
        sampling: SamplingParams | None = None,
        signal: CancelToken | None = None,
        clock: Callable[[], int] | None = None,
        emit: Callable[[str], None] | None = None,
        observer: LoopObserver | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._model = model
        self._session_id = session_id
        self._workspace_root = workspace_root
        self._max_rounds = max_rounds
        self._sampling = sampling
        self._signal = signal
        # 时钟可注入：回放测试要求"两次执行逐字节一致"，
        # 而真实时钟每次不同——不注入就永远无法满足那条断言。
        self._clock: Callable[[], int] = clock or (lambda: int(time.time()))
        self._emit = emit
        # 观测是**旁听**：为 None 时 loop 的行为与加观测之前完全一致
        # （门槛 G33：226 个既有用例就是这条的证据）。
        self._observer = observer

    def _notify(self, event: LoopEvent) -> None:
        """向观察者发一个事件。没有观察者时这是一次空调用。"""
        if self._observer is not None:
            self._observer.on_event(event)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run_turn(self, messages: list[AgentMessage]) -> TurnResult:
        """跑一轮，直到模型不再请求工具调用，或达到轮数上限。

        ⚠️ ``messages`` **必须是 agent 层消息**（``LlmMessageWrapper`` /
        ``ToolResultAgentMessage``），**不是**裸的 ``UserMessage``。

        传 LLM 层消息不会报错：``convert_to_llm`` 认不出它（LLM 层消息没有
        ``to_llm()``），于是**丢弃并发一条 warning**，而 loop 会把
        "历史为空"当作合法输入正常跑完。**症状是"模型从没看到历史"，
        但进程与测试全绿。**

        这里**不加运行期类型检查**，理由与批次 1.5 的 W1 抉择一致：
        "认不出的消息"在扩展场景下是正常现象，一刀切抛错会把合法的扩展也拦掉。
        改由 warning 兜住，并由这条 docstring 提醒。
        """
        produced: list[AgentMessage] = []
        last_text = ""
        # ⚠️ 这里**累加**，不是"赋最后一轮的值"（2026-09-21 修）。
        #
        # 原写法是 `total_usage = assistant.usage`——变量名叫 total，
        # 装的却是**最后一轮**的数字。它不报错、类型也对，所以一直没被看见；
        # 直到 `evals/runner.py` 拿它当"每任务 token"时才暴露：
        # 一个 6 轮的任务，报告里显示的 prompt token 只是第 6 轮的。
        #
        # 为什么必须累加：`evals/README.md` 把「每任务 token」列为指标，
        # 而 `TurnResult.usage` 是它**唯一**的数据来源。
        # 口径写成"最后一轮"会让"多轮任务更贵"这个基本事实在报告里消失。
        total_usage: Usage | None = None

        for round_index in range(1, self._max_rounds + 1):
            # 第 2 步 transformContext：P1 没有钩子体系（属 P3），此处跳过。
            # 保留这个注释是为了让 P3 接手时能一眼看到接入点在哪。
            llm_messages = self._to_llm(messages, produced)  # 第 3 步

            assistant, calls = await self._stream_model(llm_messages)  # 第 4 步
            produced.append(_wrap(assistant))
            last_text = _text_of(assistant)
            total_usage = _add_usage(total_usage, assistant.usage)

            if not calls:  # 第 8 步：模型不再要工具 → 完成
                finished = TurnResult(
                    status="completed",
                    messages=produced,
                    text=last_text,
                    rounds=round_index,
                    usage=total_usage,
                )
                self._notify(_turn_end(finished))
                return finished

            # 第 5、6 步：校验参数并执行完整批次
            results = await self._execute_batch(calls)

            # 第 7 步：逐个追加工具结果。
            # ``strict=True`` 要求数量严格相等——少一个立刻抛，不静默放过。
            for item, result in zip(calls, results, strict=True):
                if not item.ok:
                    # 拼装失败的调用：构造一条说明性结果，让模型知道
                    # **它上一次的调用没有被接受**（否则它会以为自己已经调过了）
                    # 观测上也要发一条失败——否则终端在这一步什么都不会显示
                    self._notify(
                        ToolEnd(
                            name="(unparsed)",
                            ok=False,
                            preview=_preview(item.parse_error),
                        )
                    )
                    produced.append(_failure_message(item, self._clock()))
                    continue
                self._notify(
                    ToolEnd(
                        name=item.name or "?",
                        ok=not result.is_error,
                        preview=_preview(_first_text(result)),
                    )
                )
                produced.append(
                    ToolResultAgentMessage.from_result(
                        item.to_block(), result, timestamp=self._clock()
                    )
                )

        # 轮数耗尽：不是错误，但要显式告诉调用方和用户
        stopped = TurnResult(
            status="stopped",
            messages=produced,
            text=last_text,
            rounds=self._max_rounds,
            usage=total_usage,
            reason=f"达到 max_rounds={self._max_rounds}",
        )
        self._notify(_turn_end(stopped))
        return stopped

    # ------------------------------------------------------------------
    # 第 3 步：agent 层 → LLM 层
    # ------------------------------------------------------------------

    def _to_llm(
        self, history: list[AgentMessage], produced: list[AgentMessage]
    ) -> list[LlmMessage]:
        """降级。

        必须走 ``convert_to_llm`` ——它是 agent → LLM 的唯一通道，
        由 import-linter 契约钉住。这里**不要**自己写映射。
        """
        return convert_to_llm([*history, *produced])

    # ------------------------------------------------------------------
    # 第 4 步：流式取回，聚合成一条 AssistantMessage
    # ------------------------------------------------------------------

    async def _stream_model(
        self, messages: list[LlmMessage]
    ) -> tuple[AssistantMessage, list[AssembledCall]]:
        """消费事件流，聚合成一条 assistant 消息与解析后的工具调用。

        provider 层（``sigma_ai.openai``）明确把这个聚合留给 loop：
        它只保证"分片被正确按 index 归属"，拼成消息是这里的职责。
        """
        text_parts: list[str] = []
        text_signature: str | None = None
        # 分片累积交给协议层的装配器——**这里不再自己写一遍**。
        # 那套逻辑（按 index 归属、跨片拼接、JSON 失败原因）是 wire protocol 的知识，
        # 归 `sigma_ai.tool_calls` 管（详规子项 C）。
        assembler = ToolCallAssembler()
        usage: Usage | None = None
        stop_reason: str = "stop"
        error_messages: list[str] = []

        async for event in self._provider.stream(
            messages,
            self._registry.schemas(),
            model=self._model,
            signal=self._signal,  # type: ignore[arg-type]
            sampling=self._sampling,
        ):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                # **逐块透传**：聚合后再发就没有"流式"了（observe.TextChunk 的说明）
                self._notify(TextChunk(text=event.text))
                if event.text_signature is not None:
                    text_signature = event.text_signature
            elif isinstance(event, ThinkingDelta):
                self._notify(ThinkingChunk(text=event.thinking))
            elif isinstance(event, ToolCallDelta):
                assembler.feed(event)
            elif isinstance(event, UsageEvent):
                usage = event.usage
            elif isinstance(event, StopEvent):
                stop_reason = event.stop_reason
            elif isinstance(event, ErrorEvent):
                error_messages.append(f"{event.error.code}: {event.error.message}")

        calls = assembler.finish()

        blocks: list[ContentBlock] = []
        text = "".join(text_parts)
        if text:
            blocks.append(TextBlock(text=text, text_signature=text_signature))
        for item in calls:
            if item.ok:
                # 只有拼装成功的才进 assistant 消息——**失败的调用不该伪装成
                # 一次合法调用**（理由见 AssembledCall.to_block 的 docstring）
                blocks.append(item.to_block())

        assistant = AssistantMessage(
            content=blocks,
            provider=type(self._provider).__name__,
            model=self._model,
            usage=usage or Usage(prompt_tokens=0, completion_tokens=0),
            stop_reason=stop_reason,  # type: ignore[arg-type]
            error_message="; ".join(error_messages),
            timestamp=self._clock(),
        )
        return assistant, calls

    # ------------------------------------------------------------------
    # 第 5、6 步：校验并执行整批
    # ------------------------------------------------------------------

    async def _execute_batch(self, calls: list[AssembledCall]) -> list[ToolResult]:
        """执行一批工具调用。

        返回结果**与 ``calls`` 一一对应、顺序一致**（门槛 G27）。
        顺序丢了会导致 ``tool_call_id`` 与结果错配，
        而症状是"模型拿着 A 的结果回答 B 的问题"——长对话里极难定位。

        三条规则（架构 4.3 节）P1 做两条：
            ① 只读工具并发、写工具严格顺序 —— 做
            ② 每个调用过钩子 —— **不做**（P3；没有钩子的调用点是有意留空的注释）
            ③ 单个失败不中断批次 —— 做
        """
        results: list[ToolResult | None] = [None] * len(calls)
        planned: list[_Planned] = []

        for position, item in enumerate(calls):
            # 拼装失败（缺 name 或 JSON 非法）：直接给模型一条说明，不执行任何工具。
            # 这一级校验由协议层的 ToolCallAssembler 判定（详规子项 C）。
            if not item.ok:
                results[position] = ToolResult(
                    content=[TextBlock(text=item.parse_error)],
                    details={"index": item.index, "raw_arguments": item.raw_arguments},
                    is_error=True,
                )
                continue

            # 拼装成功时 name / arguments 必然非空（AssembledCall 的契约），
            # 但类型上它们是 `| None`——这里**显式收窄而不是断言**，
            # 让 mypy 继续参与检查（断言会让类型检查在这里失效）。
            tool_name = item.name or ""
            arguments = item.arguments or {}

            try:
                tool = self._registry.get(tool_name)
            except KeyError as exc:
                results[position] = ToolResult(
                    content=[TextBlock(text=str(exc))],
                    details={"tool_name": tool_name},
                    is_error=True,
                )
                continue

            # 第 5 步：schema 校验。这是**参照 Pi 补进来的一步**（详规 3.8.1）。
            try:
                validated = tool.params.model_validate(arguments)
            except ValidationError as exc:
                results[position] = ToolResult(
                    content=[
                        TextBlock(
                            text=(
                                f"工具 {tool_name} 的参数不符合 schema：\n{exc}\n"
                                f"你给的参数是：{json.dumps(arguments, ensure_ascii=False)}"
                            )
                        )
                    ],
                    details={"tool_name": tool_name},
                    is_error=True,
                )
                continue

            planned.append(
                _Planned(
                    position=position,
                    # to_block() 只在拼装成功时可调用——上面已确保这一点
                    call=item.to_block(),
                    args=validated,
                    tool=tool,
                )
            )

        # 观测：工具**即将**执行。集中在这里发，保证每个 ToolStart 都早于任何
        # ToolEnd——顺序错了，终端上就会显示"先出结果后出调用"。
        for plan in planned:
            self._notify(
                ToolStart(
                    name=plan.tool.name,
                    arguments=plan.call.arguments,
                    call_id=plan.call.id,
                )
            )

        # 规则①：只读并发、写工具严格顺序。
        # 写工具并发是不确定性的来源，而"确定性回放"是整个评测的地基。
        readonly = [p for p in planned if p.tool.read_only]
        writers = [p for p in planned if not p.tool.read_only]

        if readonly:
            gathered = await asyncio.gather(
                *(p.tool.run(p.args, self._make_context()) for p in readonly),
                return_exceptions=True,
            )
            for plan, outcome in zip(readonly, gathered, strict=True):
                results[plan.position] = _as_result(outcome, plan.tool.name)

        for plan in writers:
            try:
                results[plan.position] = await plan.tool.run(
                    plan.args, self._make_context()
                )
            except Exception as exc:  # 兜底：工具没接住的异常在此转成模型可见的结果
                # loop 再兜一层：工具自己没接住的异常在这里转成模型可见的结果。
                # **绝不让异常穿透到 loop 之外**——那会中断整个批次。
                results[plan.position] = ToolResult(
                    content=[
                        TextBlock(
                            text=f"工具 {plan.tool.name} 抛出未捕获异常："
                            f"{type(exc).__name__}: {exc}"
                        )
                    ],
                    details={"tool_name": plan.tool.name},
                    is_error=True,
                )

        # 到这里每个位置都应该有结果；若没有，说明上面的分支漏了一种。
        # **宁可崩，不要错**：返回一个 None 会让下游 unpack 时才炸，症状远离根因。
        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            raise RuntimeError(
                f"_execute_batch 有位置没有结果：{missing}。"
                "这是 loop 自身的缺陷——上面的分支没有覆盖全部情况。"
            )
        return [r for r in results if r is not None]

    # ------------------------------------------------------------------

    @property
    def _workspace(self) -> Path:
        return Path(self._workspace_root) if self._workspace_root else Path.cwd()

    def _make_context(self) -> ToolContext:
        return ToolContext(
            session_id=self._session_id,
            workspace_root=self._workspace,
            signal=self._signal,  # type: ignore[arg-type]
            emit=self._emit,
        )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _wrap(assistant: AssistantMessage) -> AgentMessage:
    """把 LLM 层消息包成 agent 层消息。"""
    return LlmMessageWrapper(timestamp=assistant.timestamp, message=assistant)


def _add_usage(acc: Usage | None, delta: Usage) -> Usage:
    """把一轮的用量累加到总计上。``acc`` 为 None 时表示这是第一轮。

    **三个字段都要加，不能只加 prompt/completion**：
    ``cached_tokens`` 是 D4 的核心指标（prompt cache 命中率的分母/分子），
    漏掉它会让"常驻区稳定"这条主张在报告里失去数据支撑。
    """
    if acc is None:
        return delta
    return Usage(
        prompt_tokens=acc.prompt_tokens + delta.prompt_tokens,
        completion_tokens=acc.completion_tokens + delta.completion_tokens,
        cached_tokens=acc.cached_tokens + delta.cached_tokens,
    )


def _text_of(assistant: AssistantMessage) -> str:
    """抽出 assistant 消息里的纯文本（用于 TurnResult.text）。"""
    parts = [b.text for b in assistant.content if isinstance(b, TextBlock)]
    return "".join(parts)


TOOL_PREVIEW_CHARS = 200
"""终端 / 观测里展示的工具结果预览长度。

工具结果本体可以很大（截断上限 8 KB），把它整段 echo 到终端等于
**把上下文预算花在 UI 上**。预览只服务于"人想知道发生了什么"。
"""


def _first_text(result: ToolResult) -> str:
    """取结果里第一段文本，用于预览。"""
    for block in result.content:
        if isinstance(block, TextBlock):
            return block.text
    return ""


def _preview(text: str) -> str:
    """把工具结果压成一行预览：换行折叠成空格，超长截断。"""
    flat = " ".join(text.split())
    if len(flat) <= TOOL_PREVIEW_CHARS:
        return flat
    return flat[:TOOL_PREVIEW_CHARS] + " …"


def _turn_end(result: TurnResult) -> TurnEnd:
    """把 ``TurnResult`` 转成观测事件。带 usage 是为了让终端提示上下文压力（R1）。"""
    usage = result.usage
    return TurnEnd(
        status=result.status,
        rounds=result.rounds,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
    )


def _failure_message(item: AssembledCall, timestamp: int) -> ToolResultAgentMessage:
    """为「拼装失败的工具调用」构造一条 agent 层消息。

    为什么要构造消息、而不是直接丢掉：
    模型需要知道**它上一次的调用没有被接受**，否则它会以为自己已经调过了，
    于是要么重复调用，要么基于"工具没返回"继续往下走。

    这与 G14 的「未知消息类型 warning + 丢弃」是**不同场景**：
    那里丢的是**别人的**消息，这里回的是**模型自己刚发出来的**调用。
    """
    return ToolResultAgentMessage(
        tool_call_id=f"unparsed_{item.index}",
        tool_name="(unparsed)",
        content=[TextBlock(text=item.parse_error)],
        details={"raw_arguments": item.raw_arguments},
        is_error=True,
        timestamp=timestamp,
    )


# `_parse_call` 已于 2026-09-20 删除——它的职责（分片拼装 + JSON 一级校验）
# 整体迁到了 `sigma_ai.tool_calls.ToolCallAssembler`。
#
# 迁移的理由不只是"把代码搬个地方"：那份逻辑原本**存在两份实现**
# （provider 内部一份、这里一份），而**多工具 index 交错到达**
# 恰恰是最容易写错的地方。收进协议层后只剩一份，
# 且 provider 与 loop 共用同一个类（详规子项 C）。


def _as_result(outcome: ToolResult | BaseException, tool_name: str) -> ToolResult:
    """把 ``asyncio.gather`` 的产出（可能是异常）统一成 ``ToolResult``。"""
    if isinstance(outcome, ToolResult):
        return outcome
    if isinstance(outcome, BaseException):
        return ToolResult(
            content=[
                TextBlock(
                    text=f"工具 {tool_name} 抛出未捕获异常："
                    f"{type(outcome).__name__}: {outcome}"
                )
            ],
            details={"tool_name": tool_name},
            is_error=True,
        )
    # gather 的返回类型理论上只有 ToolResult 或异常；走到这里说明工具
    # 返回了别的东西——那是工具实现违反了 BaseTool.run 的契约。
    return ToolResult(
        content=[
            TextBlock(
                text=f"工具 {tool_name} 返回了 {type(outcome).__name__} 而不是 ToolResult，"
                "违反 BaseTool.run 的契约。"
            )
        ],
        details={"tool_name": tool_name},
        is_error=True,
    )
