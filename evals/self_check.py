"""自证报告汇总器:把 7.6 主张的零成本验证行汇成 `evals/reports/self_check.md`。

定位(P5-批次2 波 1,拍板 Q1)
    每一行都由**本脚本真实运行**生成(不存在写死的数字——注入实验
    钉的就是这条);"主张→数据→口径→诚实声明"四列,诚实声明列是
    架构 6.3/R6 的要求:口径说不清的数据不配进表。

行清单(波 1 范围)
    主张 4  缓存命中率   ← 真实会话 JSONL 的 usage 累计(本文件 cache_stats)
    主张 6  回滚成功率   ← rollback_stats.run_rollback_stats(30)
    主张 7  拦截/误拦    ← gate_replay.run_gate_replay()(回放口径,评闸不评模型)
    主张 8f steering    ← 本文件 run_steering_scenarios()(回放口径)
    主张 8b 技能渐进披露 ← 本文件 skill_token_delta()(estimate_text 粗估)
    引用行              ← todo_ab / rounds_scan / p1_gate3(既有报告,不重跑)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sigma.agent.messages import LlmMessageWrapper
from sigma.hooks.base import (
    HookManager,
)
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.registry import ToolRegistry
from sigma.providers.base import BaseProvider, CancelToken
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import LlmMessage, UserMessage
from sigma.providers.stamps import from_epoch as ts
from sigma.providers.tokens import estimate_text
from sigma.sessions.context import SessionContext
from sigma.hooks.persist import SessionPersistHook
from sigma.sessions.sessions import TRACE_SUFFIX
from sigma.sessions.store import JsonlStore
from sigma.sessions.tree import SessionTree
from sigma.cli.main import (
    DEFAULT_SESSIONS_DIR,
)
from sigma.sdk import build_system_prompt

REPO = Path(__file__).resolve().parent.parent
EVALS_DIR = Path(__file__).resolve().parent
# 与 task_runner.py / runner.py 同款:脚本直跑时手动补路径
# (editable 安装只覆盖 core/,evals 兄弟模块不在这条路上)。
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(EVALS_DIR))

from gate_replay import run_gate_replay  # noqa: E402
from rollback_stats import run_rollback_stats  # noqa: E402

REPORTS = EVALS_DIR / "reports"
FIXED_TIME = ts(1_700_000_000)
STEERING_SCENARIOS = 3


# ---------------------------------------------------------------------------
# 主张 4:缓存命中率(真实会话,零成本)
# ---------------------------------------------------------------------------


@dataclass
class SessionCacheRow:
    session_id: str
    rounds: int
    prompt_tokens: int
    cached_tokens: int

    @property
    def rate(self) -> float | None:
        if self.prompt_tokens <= 0:
            return None
        return self.cached_tokens / self.prompt_tokens


@dataclass
class CacheStats:
    sessions: list[SessionCacheRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    total_prompt: int = 0
    total_cached: int = 0

    @property
    def rate(self) -> float | None:
        if self.total_prompt <= 0:
            return None
        return self.total_cached / self.total_prompt


def cache_stats(sessions_dir: Path) -> CacheStats:
    """扫会话目录,逐会话累计 usage(经 build_timeline 同一解析面)。

    汇总 = **全部会话累加**,不是"最近一个"——2026-09-21 那个
    "变量名叫 total 装的却是最后一轮"的 bug 就是这条门槛要防的形状。
    """
    stats = CacheStats()
    if not sessions_dir.is_dir():
        return stats
    for path in sorted(sessions_dir.glob("*.jsonl")):
        if path.name.endswith(TRACE_SUFFIX):
            continue
        session_id = path.name[: -len(".jsonl")]
        try:
            row = _session_cache_row(session_id, path)
        except Exception as exc:  # 坏会话跳过并如实记名,不让一个文件拖垮汇总
            # 错误详情截单行:pydantic 的多行堆栈会把报告撑爆,
            # 跳过行只需要"哪个文件、为什么类型的问题"。
            first_line = str(exc).splitlines()[0][:120]
            stats.skipped.append(f"{session_id}: {type(exc).__name__}: {first_line}")
            continue
        if row is None:
            stats.skipped.append(f"{session_id}: 无 assistant 消息")
            continue
        stats.sessions.append(row)
        stats.total_prompt += row.prompt_tokens
        stats.total_cached += row.cached_tokens
    return stats


def _session_cache_row(session_id: str, path: Path) -> SessionCacheRow | None:
    from sigma.observability.timeline import build_timeline

    timeline = build_timeline(session_id, path, None)
    if not timeline.rounds:
        return None
    return SessionCacheRow(
        session_id=session_id,
        rounds=len(timeline.rounds),
        prompt_tokens=timeline.total_prompt,
        cached_tokens=timeline.total_cached,
    )


# ---------------------------------------------------------------------------
# 主张 8b:技能渐进披露的常驻区代价(粗估,零成本)
# ---------------------------------------------------------------------------


def skill_token_delta() -> dict[str, int]:
    """技能索引进常驻区的 token 增量(estimate_text 粗估口径)。"""
    common: dict[str, Any] = dict(web_search=False, web_fetch=False, task=True, todo=True)
    without = estimate_text(build_system_prompt(skills=False, **common))
    with_idx = estimate_text(build_system_prompt(skills=True, **common))
    return {
        "resident_without_skills": without,
        "resident_with_index": with_idx,
        "index_delta": with_idx - without,
    }


# ---------------------------------------------------------------------------
# 主张 8f:steering 送达(回放口径)
# ---------------------------------------------------------------------------


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class RecordingProvider(BaseProvider):
    """包装 FakeProvider:记录每次 stream 收到的消息数组(可见性断言用)。"""

    def __init__(self, inner: BaseProvider) -> None:
        self.inner = inner
        self.seen: list[list[LlmMessage]] = []

    async def stream(  # type: ignore[override]
        self, messages, tools, *, model, signal, sampling=None, options=None, timeout_s=None
    ):
        self.seen.append(list(messages))
        async for event in self.inner.stream(
            messages,
            tools,
            model=model,
            signal=signal,
            sampling=sampling,
            options=options,
            timeout_s=timeout_s,
        ):
            yield event

    def estimate_tokens(self, messages: list[LlmMessage]) -> int:
        return self.inner.estimate_tokens(messages)


def _steering_round(command: str, call_id: str) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "tool_call_delta",
                "index": 0,
                "id": call_id,
                "name": "bash",
                "arguments_delta": json.dumps({"command": command}),
            },
            {"type": "stop", "stop_reason": "tool_use"},
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 0},
    }


def _final_round(text: str) -> dict[str, Any]:
    return {
        "events": [
            {"type": "text_delta", "text": text},
            {"type": "stop", "stop_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 150, "completion_tokens": 20, "cached_tokens": 100},
    }


@dataclass
class SteeringCase:
    """一次运行的事实:最终文本、轮数、steering 是否送达/落盘、与基线的一致性。"""

    text: str
    rounds: int
    delivered: bool
    persisted: bool
    text_unchanged: bool = True
    rounds_equal: bool = True

    @property
    def ok(self) -> bool:
        return self.delivered and self.persisted and self.text_unchanged and self.rounds_equal


def _run_steering_case(
    transcript: Path,
    workspace: Path,
    session_tmp: Path,
    correction: str | None,
) -> SteeringCase:
    session_tmp.mkdir(parents=True, exist_ok=True)
    context = SessionContext(
        system_prompt="评测",
        tools_schema=[],
        clock=lambda: FIXED_TIME,
        tree=SessionTree(store=JsonlStore(session_tmp, "s1")),
    )
    hooks = HookManager()
    hooks.register(SessionPersistHook(context))

    provider = RecordingProvider(FakeProvider.from_file(transcript))
    pending = [correction] if correction else []

    def drain() -> list[Any]:
        if not pending:
            return []
        text = pending.pop(0)
        return [
            LlmMessageWrapper(
                message=UserMessage(content=text, timestamp=FIXED_TIME),
                timestamp=FIXED_TIME,
            )
        ]

    from sigma.tools.builtin.bash import BashTool

    registry = ToolRegistry()
    registry.register(BashTool())
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        model="fake",
        workspace_root=workspace,
        max_rounds=10,
        signal=_NeverCancelled(),
        clock=lambda: FIXED_TIME,
        hooks=hooks,
        steering_drain=drain,
    )
    result = asyncio.run(loop.run_turn([]))

    delivered = False
    if correction and len(provider.seen) >= 2:
        for message in provider.seen[1]:
            content = getattr(message, "content", "")
            if isinstance(content, str) and correction in content:
                delivered = True
    persisted = correction is not None and any(
        correction in str(getattr(m, "message", "")) for m in context.tree.history()
    )
    return SteeringCase(
        text=result.text, rounds=result.rounds, delivered=delivered, persisted=persisted
    )


def run_steering_scenarios(base_tmp: Path | None = None) -> list[SteeringCase]:
    """3 个场景:轮中注入一条"错误方向"steering,断言送达/落盘/不破坏结果。

    口径(诚实声明,报告引用):回放验证的是**送达时机与可见性**——
    steering 在下一轮进入模型输入、落盘进会话树、最终文本与基线一致;
    "模型会不会采纳纠正"是模型能力,由转录示范,不是本行能测的。
    """
    scenarios = [
        ("wrong-dir", "ls src/", "改看 tests/", "看完了,没有需要的。"),
        ("wrong-tool", "bash echo a", "别用 bash,先读文件", "已改为读文件完成。"),
        ("wrong-scope", "grep TODO src/", "只看 mod.py,别扫全库", "已限定范围完成。"),
    ]
    owned = base_tmp is None
    tmp = Path(tempfile.mkdtemp(prefix="sigma-steer-")) if base_tmp is None else base_tmp
    tmp.mkdir(parents=True, exist_ok=True)  # 注入的 base_tmp 也可能是未建的路径
    cases: list[SteeringCase] = []
    try:
        for name, cmd, correction, final_text in scenarios:
            transcript = tmp / f"{name}.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(r, ensure_ascii=False)
                    for r in (
                        _steering_round(cmd, f"call_{name}-1"),
                        _steering_round(cmd, f"call_{name}-2"),
                        _final_round(final_text),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            workspace = tmp / f"ws-{name}"
            workspace.mkdir(parents=True, exist_ok=True)
            baseline = _run_steering_case(transcript, workspace, tmp / f"base-{name}", None)
            steered = _run_steering_case(
                transcript, workspace, tmp / f"steer-{name}", correction
            )
            steered.text_unchanged = steered.text == baseline.text
            steered.rounds_equal = steered.rounds == baseline.rounds
            cases.append(steered)
    finally:
        if owned:
            shutil.rmtree(tmp, ignore_errors=True)
    return cases


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def build_report(sessions_dir: Path) -> tuple[str, bool]:
    """跑全部波 1 验证,生成 self_check.md。返回 (markdown, 是否全部达标)。"""
    lines: list[str] = []
    all_ok = True
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines.append("# sigma 自证报告(self_check)——7.6 主张的零成本验证行")
    lines.append("")
    lines.append(f"> 生成:{stamp}(每行由本脚本真实运行生成,无写死数字)")
    lines.append(">")
    lines.append("> 口径总则:能回放的不花 API 钱;口径说不清的行不进表。")
    lines.append("")

    # 主张 4:缓存命中率
    stats = cache_stats(sessions_dir)
    if stats.sessions and stats.rate is not None:
        data = (
            f"cache 命中率 **{stats.rate:.1%}**"
            f"({len(stats.sessions)} 个真实会话 / {sum(s.rounds for s in stats.sessions)} 轮"
            + (f";跳过:{';'.join(stats.skipped)}" if stats.skipped else "，无跳过")
            + ")"
        )
    else:
        data = (
            f"无数据({sessions_dir} 无可用会话"
            + (f";跳过 {len(stats.skipped)}" if stats.skipped else "")
            + ")"
        )
        all_ok = False
    lines.append("## 主张 4|常驻区稳定能吃到缓存")
    lines.append(f"- **数据**:{data}")
    lines.append("- **口径**:cached_tokens / prompt_tokens 累计,取 provider 返回的真实 usage")
    lines.append(
        "- **诚实声明**:样本=本机现有会话(非受控实验);命中率阈值待 P5 收尾实测后定值,本行只报数不下结论"
    )
    lines.append("")

    # 主张 6:回滚成功率
    rollback = run_rollback_stats(iterations=30)
    ok6 = rollback.all_ok
    all_ok = all_ok and ok6
    lines.append("## 主张 6|checkpoint 是有效边界")
    lines.append(
        f"- **数据**:回滚成功率 **{rollback.successes}/{rollback.snapshots_checked}**"
        f"(写批次 {rollback.iterations} 次,改/增/删交错)"
    )
    lines.append("- **口径**:纯 git 层,恢复后逐文件比对内容(不只比文件名)")
    lines.append(
        "- **诚实声明**:不经模型;不含模型写后自改的竞态;>5MB 与排除清单文件不参与快照"
    )
    if not ok6:
        lines.append(f"- ⚠ 失败:{'; '.join(rollback.failures[:5])}")
    lines.append("")

    # 主张 7:拦截率/误拦率
    gate = run_gate_replay()
    ok7 = not gate.failures
    all_ok = all_ok and ok7
    lines.append("## 主张 7|钩子能减少危险操作")
    lines.append(
        f"- **数据**:拦截 **{gate.denied}/{gate.deny_total}** · "
        f"误拦 **{gate.false_blocks}/{gate.allow_total}** · "
        f"allowlist 免打扰 **{gate.allowlist_hits}/{gate.allowlist_total}**"
    )
    lines.append("- **口径**:回放驱动**真实审批层**(CliApprovalGate),chooser 桩恒拒绝")
    lines.append(
        "- **诚实声明**:评的是**闸**不是模型——对抗样本为危险命令的字符串形态"
        "(echo 前缀,放行也无害);真实模型是否发出这些调用是另一问题。"
        "首版实测曾揪出\"相对 cwd 按进程目录误判越界\"的缺陷并已修(案例 a08)"
    )
    if not ok7:
        lines.append(f"- ⚠ 失败:{'; '.join(gate.failures[:5])}")
    lines.append("")

    # 主张 8f:steering
    cases = run_steering_scenarios()
    ok8f = len(cases) == STEERING_SCENARIOS and all(case.ok for case in cases)
    all_ok = all_ok and ok8f
    lines.append("## 主张 8f|steering 送达")
    lines.append(
        f"- **数据**:{sum(1 for c in cases if c.ok)}/{len(cases)} 场景送达且结果不变"
        "(下一轮模型输入可见 + 落盘进会话树 + 最终文本与基线一致)"
    )
    lines.append("- **口径**:回放口径——验证送达时机与可见性")
    lines.append(
        "- **诚实声明**:\"模型采纳纠正\"是模型能力,由转录示范;本行只证 harness 的送达语义"
    )
    lines.append("")

    # 主张 8b:技能 token
    skill = skill_token_delta()
    lines.append("## 主张 8b|技能渐进披露(按需加载)")
    lines.append(
        f"- **数据**:技能索引使常驻区 **+{skill['index_delta']} token**"
        f"({skill['resident_without_skills']} → {skill['resident_with_index']});"
        "正文 0(按需经 load_skill 进尾部)"
    )
    lines.append("- **口径**:estimate_text 粗估(与 5.1 节实测同口径)")
    lines.append(
        "- **诚实声明**:粗估;正文按需不破缓存是**结构性质**(索引在常驻区、正文在尾部),非本行测得"
    )
    lines.append("")

    # 引用既有报告
    lines.append("## 既有报告引用(不重跑)")
    lines.append(
        "- todo 消融:reports/todo_ab_report.md ｜ 轮数扫描:reports/rounds_scan_report.md ｜ "
        "B1 vs B2 翻转门:reports/p1_gate3.md(10 条口径;正式集 30 条=波 2,待跑)"
    )
    lines.append("")

    return "\n".join(lines), all_ok


def main() -> int:
    sessions_dir = DEFAULT_SESSIONS_DIR
    if "--sessions-dir" in sys.argv:
        sessions_dir = Path(sys.argv[sys.argv.index("--sessions-dir") + 1])
    markdown, all_ok = build_report(sessions_dir)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "self_check.md").write_text(markdown, encoding="utf-8")
    print(f"self_check.md 已生成({'全部达标' if all_ok else '存在失败行,见报告 ⚠'})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
