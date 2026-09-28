"""观测层·查看端：把会话 JSONL（+ 可选 trace 文件）渲染成执行时间线。

数据源与优先级（P5-批次1，详规 §2.4）
    会话 JSONL 是**主数据源**——每条消息带毫秒时间戳与 usage，轮次、
    token、缓存命中、截断、工具错误都从它来，**所以旧会话（没有
    trace 文件）也能查**。trace 文件存在时叠加两类 JSONL 没有的信息：
    精确的单次调用延迟与 TTFT（``LlmRequested`` 配对）、审批决策留痕。

    没有 trace 时，单轮延迟用**相邻消息时间戳差**近似——上一条消息
    （工具结果/注入/上一轮 assistant）到本条 assistant 之间只有模型调用，
    差值 ≈ 调用耗时；显示时带 ``≈``、TTFT 留空，**不冒充精确值**。

指标口径
    全部对齐 ``architecture.md`` 7.5：轮数、每任务 token（prompt/
    completion/cached）、cache 命中率（cached/prompt）、截断发生率、
    工具错误数。**不发明新指标**——本查看器读的就是 P5 ablation
    要报的那些数（``--json`` 输出供 ablation 脚本复用）。

纯函数
    本模块除读文件外无副作用、不 import rich——渲染成纯文本行，
    打印方式（Panel/裸 print/测试断言）归调用方。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigma_agent.agent_messages import LlmMessageWrapper, ToolResultAgentMessage
from sigma_ai.messages import AssistantMessage
from sigma_ai.stamps import to_epoch
from sigma_session.store import JsonlStore
from sigma_session.tree import SessionTree


@dataclass(frozen=True)
class ToolView:
    """一轮里的一个工具调用。duration 只有 trace 在场才有。"""

    name: str
    ok: bool
    duration_ms: int | None = None


@dataclass(frozen=True)
class RoundView:
    """一轮 = 一次 LLM 调用 + 它的工具批次（与 loop 的 round 同口径）。"""

    index: int
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int | None = None
    latency_approx: bool = False
    ttft_ms: int | None = None
    tools: tuple[ToolView, ...] = ()


@dataclass(frozen=True)
class ApprovalView:
    """一次审批结论。reason 保原文，截断在渲染层做。"""

    name: str
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class TimelineReport:
    session_id: str
    rounds: tuple[RoundView, ...]
    total_prompt: int = 0
    total_completion: int = 0
    total_cached: int = 0
    tool_errors: int = 0
    truncated: int = 0
    approvals: tuple[ApprovalView, ...] = ()
    injected: int = 0
    wall_seconds: float | None = None
    wall_approx: bool = False
    status: str = ""
    has_trace: bool = False

    @property
    def cache_rate(self) -> float | None:
        """cache 命中率 = cached / prompt（7.5 口径）。prompt 为 0 时不可用。"""
        if self.total_prompt <= 0:
            return None
        return self.total_cached / self.total_prompt


def build_timeline(
    session_id: str, session_jsonl: Path, trace_path: Path | None
) -> TimelineReport:
    """从会话文件（+ 可选 trace）构建时间线报告。

    坏行容错沿用 ``SessionTree.from_store``（跳过并记行号）；trace 文件
    逐行解析、坏行跳过——查看器是只读的，坏数据降级展示，不抛给用户。
    """
    tree = SessionTree.from_store(JsonlStore(session_jsonl.parent, session_id))
    rounds: list[dict[str, Any]] = []
    total_prompt = total_completion = total_cached = 0
    tool_errors = truncated = 0
    approvals: list[ApprovalView] = []
    injected = 0
    prev_epoch: float | None = None
    first_epoch: float | None = None
    last_epoch: float | None = None

    for message in tree.history():
        epoch = _safe_epoch(message.timestamp)
        if epoch is not None:
            if first_epoch is None:
                first_epoch = epoch
            last_epoch = epoch
        if isinstance(message, LlmMessageWrapper) and isinstance(
            message.message, AssistantMessage
        ):
            # 只有内层是 AssistantMessage 的 wrapper 才是一轮——
            # 包装 user 消息的 wrapper（落盘形态）不是模型调用。
            assistant = message.message
            model = assistant.model
            prompt = assistant.usage.prompt_tokens
            completion = assistant.usage.completion_tokens
            cached = assistant.usage.cached_tokens
            latency: int | None = None
            if prev_epoch is not None and epoch is not None:
                latency = max(0, round((epoch - prev_epoch) * 1000))
            total_prompt += prompt
            total_completion += completion
            total_cached += cached
            rounds.append(
                {
                    "model": model,
                    "prompt": prompt,
                    "completion": completion,
                    "cached": cached,
                    "latency": latency,
                    "ttft": None,
                    "approx": True,
                    "tools": [],
                }
            )
        elif isinstance(message, ToolResultAgentMessage):
            if rounds:
                rounds[-1]["tools"].append(
                    ToolView(name=message.tool_name, ok=not message.is_error)
                )
            if message.is_error:
                tool_errors += 1
            if message.details.get("truncated"):
                truncated += 1
            # trace 缺失时的审批兜底：会话文件只留得住"拒绝"的影子
            # （details 标记），放行不可见——如实标注，不假装完整。
            if message.details.get("approval") == "denied":
                approvals.append(
                    ApprovalView(
                        name=message.tool_name,
                        allowed=False,
                        reason="（trace 缺失，由会话文件的拒绝结果反推；放行不可见）",
                    )
                )
        if epoch is not None:
            prev_epoch = epoch

    wall_seconds: float | None = None
    wall_approx = True
    status = ""
    has_trace = trace_path is not None and trace_path.is_file()
    if has_trace and trace_path is not None:
        lines = _read_trace(trace_path)
        # 审批/注入/状态以 trace 为准（比 JSONL 兜底完整：放行也留痕）。
        approvals = []
        trace_round = 0
        # 每轮的 tool_end 时长按到达顺序收集，最后与 JSONL 的工具序
        # 按下标 zip——两侧是同一发射序列，序天然一致。
        tool_durations: dict[int, list[int]] = {}
        t_first: float | None = None
        t_last: float | None = None
        for entry in lines:
            t = entry.get("t")
            if isinstance(t, (int, float)):
                if t_first is None:
                    t_first = float(t)
                t_last = float(t)
            kind = entry.get("kind")
            if kind == "llm_requested":
                trace_round += 1
            elif kind == "llm_end":
                if 0 < trace_round <= len(rounds):
                    slot = rounds[trace_round - 1]
                    latency = entry.get("latency_ms")
                    if isinstance(latency, (int, float)):
                        slot["latency"] = round(float(latency))
                        slot["approx"] = False
                    ttft = entry.get("ttft_ms")
                    if isinstance(ttft, (int, float)):
                        slot["ttft"] = round(float(ttft))
            elif kind == "tool_end":
                duration = entry.get("duration_ms")
                if isinstance(duration, (int, float)):
                    tool_durations.setdefault(trace_round, []).append(
                        round(float(duration))
                    )
            elif kind == "approval":
                approvals.append(
                    ApprovalView(
                        name=str(entry.get("name", "?")),
                        allowed=bool(entry.get("allowed")),
                        reason=str(entry.get("reason", "")),
                    )
                )
            elif kind == "injected":
                injected += 1
            elif kind == "turn_end":
                status = str(entry.get("status", ""))
        for round_no, durations in tool_durations.items():
            if not (0 < round_no <= len(rounds)):
                continue
            tools = rounds[round_no - 1]["tools"]
            paired: list[ToolView] = []
            for i, tool in enumerate(tools):
                # 下标越界（trace 与会话文件不配套）就丢弃时长，不猜。
                paired.append(
                    ToolView(
                        name=tool.name,
                        ok=tool.ok,
                        duration_ms=durations[i] if i < len(durations) else None,
                    )
                )
            rounds[round_no - 1]["tools"] = paired
        if t_first is not None and t_last is not None:
            wall_seconds = round(t_last - t_first, 3)
            wall_approx = False
    elif first_epoch is not None and last_epoch is not None:
        wall_seconds = round(max(0.0, last_epoch - first_epoch), 3)

    view_rounds = tuple(
        RoundView(
            index=i + 1,
            model=r["model"],
            prompt_tokens=r["prompt"],
            completion_tokens=r["completion"],
            cached_tokens=r["cached"],
            latency_ms=r["latency"],
            latency_approx=r["approx"],
            ttft_ms=r["ttft"],
            tools=tuple(r["tools"]),
        )
        for i, r in enumerate(rounds)
    )
    return TimelineReport(
        session_id=session_id,
        rounds=view_rounds,
        total_prompt=total_prompt,
        total_completion=total_completion,
        total_cached=total_cached,
        tool_errors=tool_errors,
        truncated=truncated,
        approvals=tuple(approvals),
        injected=injected,
        wall_seconds=wall_seconds,
        wall_approx=wall_approx,
        status=status,
        has_trace=has_trace,
    )


def _read_trace(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _safe_epoch(stamp: str) -> float | None:
    try:
        return to_epoch(stamp)
    except (ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _fmt_tokens(n: int) -> str:
    if n >= 1000:
        value = n / 1000
        return f"{value:.1f}k" if value < 100 else f"{value:.0f}k"
    return str(n)


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms >= 10_000:
        return f"{ms / 1000:.1f}s"
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms}ms"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_timeline(report: TimelineReport) -> list[str]:
    """渲染成纯文本行（80 列内安全；打印方式归调用方）。"""
    lines: list[str] = []
    rate = report.cache_rate
    rate_text = f"{rate:.0%}" if rate is not None else "不可用"
    lines.append(
        f"会话 {report.session_id} ｜ {len(report.rounds)} 轮 ｜ "
        f"prompt {_fmt_tokens(report.total_prompt)} / "
        f"completion {_fmt_tokens(report.total_completion)} ｜ "
        f"cache 命中 {rate_text}"
        + ("" if report.has_trace else "（无 trace 文件，延迟为近似值）")
    )
    lines.append("轮   LLM延迟    TTFT    tok 入/出/缓存         工具")
    for r in report.rounds:
        latency = _fmt_ms(r.latency_ms)
        if r.latency_approx and r.latency_ms is not None:
            latency = "≈" + latency
        tokens = (
            f"{_fmt_tokens(r.prompt_tokens)}/{_fmt_tokens(r.completion_tokens)}"
            f"/{_fmt_tokens(r.cached_tokens)}"
        )
        tools = " · ".join(
            _clip(t.name, 16)
            + (f" {_fmt_ms(t.duration_ms)}" if t.duration_ms is not None else "")
            for t in r.tools
        )
        lines.append(
            f"{r.index:>2}   {latency:<8}  {_fmt_ms(r.ttft_ms):<6}  "
            f"{tokens:<20}  {tools}"
        )
    extras: list[str] = []
    if report.approvals:
        parts = [
            f"{_clip(a.name, 16)} → {'放行' if a.allowed else '拒绝'}"
            + (f"（{_clip(a.reason, 32)}）" if a.reason and not a.allowed else "")
            for a in report.approvals
        ]
        extras.append(f"审批 {len(report.approvals)} 次：{'；'.join(parts)}")
    if report.injected:
        extras.append(f"注入 {report.injected} 次")
    if extras:
        lines.append(" ｜ ".join(extras))
    tail: list[str] = []
    if report.truncated:
        tail.append(f"截断 {report.truncated}")
    if report.tool_errors:
        tail.append(f"工具错误 {report.tool_errors}")
    if report.wall_seconds is not None:
        tail.append(("≈" if report.wall_approx else "") + f"总耗时 {report.wall_seconds:.1f}s")
    if report.status:
        tail.append(f"状态 {report.status}")
    if tail:
        lines.append(" ｜ ".join(tail))
    return lines


def timeline_to_json(report: TimelineReport) -> dict[str, Any]:
    """机器可读版（``--json``）——供 P5 ablation 脚本复用，字段与 7.5 口径同名。"""
    rate = report.cache_rate
    return {
        "session_id": report.session_id,
        "has_trace": report.has_trace,
        "rounds": len(report.rounds),
        "prompt_tokens": report.total_prompt,
        "completion_tokens": report.total_completion,
        "cached_tokens": report.total_cached,
        "cache_rate": round(rate, 4) if rate is not None else None,
        "tool_errors": report.tool_errors,
        "truncated": report.truncated,
        "injected": report.injected,
        "approvals": [
            {"name": a.name, "allowed": a.allowed, "reason": a.reason}
            for a in report.approvals
        ],
        "wall_seconds": report.wall_seconds,
        "wall_approx": report.wall_approx,
        "status": report.status,
        "per_round": [
            {
                "index": r.index,
                "model": r.model,
                "latency_ms": r.latency_ms,
                "latency_approx": r.latency_approx,
                "ttft_ms": r.ttft_ms,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "cached_tokens": r.cached_tokens,
                "tools": [
                    {"name": t.name, "ok": t.ok, "duration_ms": t.duration_ms}
                    for t in r.tools
                ],
            }
            for r in report.rounds
        ],
    }
