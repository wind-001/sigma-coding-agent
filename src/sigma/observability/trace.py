"""观测层·采集端：TraceHook——把钩子事件流落成结构化 trace 文件。

定位（P5-批次1，详规见 ``docs/plans/P5-批次1-观测层-trace与时间线-详规.md``）
    会话 JSONL 是**审计凭据**，trace 是**派生观测数据**——删掉它不损任何
    能力、不进模型上下文、不进 produced。这与 compact.py"压缩是派生视图、
    不改树"是同一条判据：**审计链与派生视图必须分家**，所以 trace 落
    独立文件（与会话文件同目录、同名不同后缀），绝不写进会话树。

失败语义（Q3 拍板）——与 SessionPersistHook **刻意相反**
    持久化失败 = 抛出、中止本轮（G92：宁可崩，审计链不能分叉）；
    trace 失败 = **警告一次 + 自禁用**，任务照跑。依据 hooks.py 既有分工：
    "宽容是订阅者的策略，不是总线的"（渲染钩子 G34/G99 同款先例）。
    分界线是数据性质，不是工程方便：

    * 持久化失败还继续跑 → 内存领先磁盘 → 审计链**分叉**（不可接受）；
    * trace 失败还继续跑 → 只少几行观测记录，无链可分叉——
      而观测挂了把任务也拖死，等于让可观测性降低可用性，方向反了。

与发射点的关系
    本钩子只**读**事件，不改变任何发射点。并发只读批次的 ``tool_end``
    落在整批收尾（ToolEnd 的发射点在 run_turn 的结果循环里，这是
    "所有 ToolStart 早于任何 ToolEnd"渲染承诺的既有代价）——于是并发
    工具的时长会互相重叠。trace 如实记录这一语义，**不为好看的数字
    去挪发射点**：观测不得扰动被观测的系统。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from sigma.agent.messages import LlmMessageWrapper, ToolResultAgentMessage
from sigma.events.lifecycle import (
    ApprovalDecided,
    AssistantProduced,
    HookEvent,
    LlmRequested,
    MessageInjected,
    TextChunk,
    ToolEnd,
    ToolStart,
    TurnEnd,
)
from sigma.hooks.base import (
    BaseHook,
)
from sigma.providers import stamps
from sigma.providers.messages import AssistantMessage
from sigma.sessions.sessions import TRACE_SUFFIX
from sigma.sessions.store import JsonlStore

#: 默认告警通道：stderr 一行。测试注入自己的收集器（G867 断言"恰一条"）。
def _warn_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def trace_path_for(sessions_dir: Path, session_id: str) -> Path:
    """会话 id → trace 文件路径（``<id>.trace.jsonl``）。

    **净化走 ``JsonlStore.path``，不自己拼**——与 ``session_path`` 同一条
    判据：净化的唯一实现在那里，抄一份必然漂移，漂移的症状是
    "trace 文件和会话文件对不上号"。后缀常量 ``TRACE_SUFFIX`` 住在
    ``sessions.py``——它必须与 ``SESSION_SUFFIX`` 的发现逻辑同源，
    否则 trace 文件会混进会话列表（幽灵会话）。
    """
    jsonl = JsonlStore(Path(sessions_dir), session_id).path
    return jsonl.with_name(jsonl.stem + TRACE_SUFFIX)


class TraceHook(BaseHook):
    """订阅全部产出事件，逐事件写一行 JSON。

    时钟**全部可注入**（``now`` 墙钟串、``timer`` 单调秒）：回放与单测
    要求确定性（G872），真实时钟永远满足不了——与 loop 的 ``clock``
    同一条纪律。
    """

    name = "trace"

    def __init__(
        self,
        session_id: str,
        sessions_dir: Path,
        *,
        now: Callable[[], str] | None = None,
        timer: Callable[[], float] | None = None,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self._session_id = session_id
        self._path = trace_path_for(Path(sessions_dir), session_id)
        self._now = now or stamps.now
        self._timer = timer or time.monotonic
        self._warn = warn or _warn_stderr
        # 自禁用墓碑（Q3）：第一次失败后置位，后续事件零尝试。
        self._dead = False
        # round 计数自维护（LlmRequested 递增）——事件保持最小载荷，
        # 不为观测给所有事件加 round 字段。
        self._round = 0
        # 在途请求：LlmRequested 起记，AssistantProduced 收口（延迟）；
        # 首个 TextChunk 记 TTFT。TextChunk 本身**不落盘**——那是流式
        # 渲染的事，trace 只借它计一个时刻。
        self._req_t: float | None = None
        self._first_text_t: float | None = None
        # 在途工具：ToolStart 按 call_id 记起点，ToolEnd 经消息上的
        # tool_call_id 配对（ToolEnd 事件本身不带 call_id）。
        self._tool_start: dict[str, float] = {}

    def events(self) -> tuple[type[HookEvent], ...]:
        return (
            LlmRequested,
            TextChunk,
            AssistantProduced,
            ToolStart,
            ToolEnd,
            MessageInjected,
            ApprovalDecided,
            TurnEnd,
        )

    # -- 落盘 ---------------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        if self._dead:
            return
        try:
            # 序列化也在 try 内：trace 自己的 bug（不可序列化字段之类）
            # 同样不配拖死任务——自宽容覆盖全部失败模式，不只是 IO。
            line = json.dumps(payload, ensure_ascii=False, default=str)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            # 自宽容（Q3）：任何失败都只值"警告一次 + 自禁用"。
            # 捕 Exception 而非 OSError 是刻意的——trace 自己的 bug
            # （序列化之类）同样不配拖死任务；症状经警告可见。
            self._dead = True
            self._warn(f"[sigma] trace 记录失败，本会话观测已停用：{exc}")
            # 墓碑行：尽力而为——文件写不进去时它多半也失败，静默即可。
            try:
                tomb = json.dumps(
                    {
                        "ts": self._now(),
                        "kind": "trace_disabled",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(tomb + "\n")
            except Exception:
                pass

    def _line(self, kind: str, t: float, **fields: Any) -> dict[str, Any]:
        return {"ts": self._now(), "t": round(t, 6), "kind": kind, **fields}

    # -- 事件处理 -----------------------------------------------------------
    # 每个事件**恰好取一次** timer——t 既是落盘字段也是测量点，
    # 一次取点让"延迟 = 两次事件 t 之差"恒成立（G866 的对齐前提）。

    def on_event(self, event: HookEvent) -> None:
        if self._dead:
            return
        t = self._timer()
        if isinstance(event, LlmRequested):
            self._round += 1
            self._req_t = t
            self._first_text_t = None
            self._write(self._line("llm_requested", t, round=self._round))
            return
        if isinstance(event, TextChunk):
            # 只计时，不写盘（流式渲染的事归渲染钩子）。
            if self._req_t is not None and self._first_text_t is None:
                self._first_text_t = t
            return
        if isinstance(event, AssistantProduced):
            self._on_assistant(event, t)
            return
        if isinstance(event, ToolStart):
            self._tool_start[event.call_id] = t
            self._write(self._line("tool_start", t, name=event.name, call_id=event.call_id))
            return
        if isinstance(event, ToolEnd):
            self._on_tool_end(event, t)
            return
        if isinstance(event, MessageInjected):
            self._write(self._line("injected", t))
            return
        if isinstance(event, ApprovalDecided):
            self._write(
                self._line(
                    "approval",
                    t,
                    name=event.name,
                    allowed=event.decision.allowed,
                    reason=event.decision.reason,
                    approve_outside=event.decision.approve_outside,
                )
            )
            return
        if isinstance(event, TurnEnd):
            self._write(
                self._line(
                    "turn_end",
                    t,
                    status=event.status,
                    rounds=event.rounds,
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                )
            )

    def _on_assistant(self, event: AssistantProduced, t: float) -> None:
        fields: dict[str, Any] = {"round": self._round}
        if self._req_t is not None:
            fields["latency_ms"] = round((t - self._req_t) * 1000)
            if self._first_text_t is not None:
                fields["ttft_ms"] = round((self._first_text_t - self._req_t) * 1000)
            self._req_t = None
            self._first_text_t = None
        # usage/model 从包装器内层取——用公开字段 isinstance 收窄，
        # 不重造包装逻辑（与 hooks.py"事件带包装后形态"的口径配套）。
        msg = event.message
        if isinstance(msg, LlmMessageWrapper) and isinstance(msg.message, AssistantMessage):
            assistant = msg.message
            fields["model"] = assistant.model
            fields["prompt_tokens"] = assistant.usage.prompt_tokens
            fields["completion_tokens"] = assistant.usage.completion_tokens
            fields["cached_tokens"] = assistant.usage.cached_tokens
        self._write(self._line("llm_end", t, **fields))

    def _on_tool_end(self, event: ToolEnd, t: float) -> None:
        fields: dict[str, Any] = {"name": event.name, "ok": event.ok}
        msg = event.message
        if isinstance(msg, ToolResultAgentMessage):
            fields["tool_call_id"] = msg.tool_call_id
            start = self._tool_start.pop(msg.tool_call_id, None)
            if start is not None:
                fields["duration_ms"] = round((t - start) * 1000)
            truncated = msg.details.get("truncated")
            if truncated is not None:
                fields["truncated"] = bool(truncated)
        self._write(self._line("tool_end", t, **fields))
