"""最小评测运行器：跑回放场景并输出报告。

**它现在能做什么、不能做什么（先说清楚）**

    能做：把 ``tests/fixtures/transcripts/`` 下的场景**真的喂进 agent loop**，
    采集「轮数 / 状态 / 工具调用数 / 工具失败数 / token / 消费完整性」，
    写成 JSON + Markdown 落到 ``evals/reports/``。

    不能做：跑 ``evals/datasets/`` 里的 70 条任务集。那些需要**真实 API key**、
    需要 checkout 真实仓库、需要判定脚本——属于后续阶段。
    **本文不假装能跑它们**（evals/README.md 明确写了"不要在这里写空壳实现"）。

**为什么先做这个而不是先做数据集**

    数据集是「问题」，运行器是「测量仪」。先有测量仪，才知道数据集跑出来的
    数字有没有意义。而且回放场景进 CI、不花钱、不联网——
    它是数据集的**前置条件**，不是替代品。

**与 CI 的关系**

    ``pytest tests/test_transcript_scenarios.py`` 是门禁的一部分：
    它断言每个场景的期望值。本文件是**评测侧**的对应物，
    额外产出可提交的报告。两者共用同一份清单（``_scenarios.py``），
    **不各写一份**——两份实现意味着修一个忘另一个。

用法
    ./.venv/Scripts/python.exe evals/runner.py
    ./.venv/Scripts/python.exe evals/runner.py --scenario read_then_edit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(REPO / "tests"))

from sigma_agent.agent_messages import LlmMessageWrapper, convert_to_llm  # noqa: E402
from sigma_agent.loop import AgentLoop  # noqa: E402
from sigma_agent.registry import ToolRegistry  # noqa: E402
from sigma_ai.base import CancelToken  # noqa: E402
from sigma_ai.fake import FakeProvider  # noqa: E402
from sigma_ai.stamps import from_epoch as ts  # noqa: E402
from sigma_ai.messages import UserMessage  # noqa: E402
from sigma_tools.bash import BashTool  # noqa: E402
from sigma_tools.edit import EditTool  # noqa: E402
from sigma_tools.grep import GrepTool  # noqa: E402
from sigma_tools.read import ReadTool  # noqa: E402
from sigma_tools.write import WriteTool  # noqa: E402

from fixtures.transcripts._scenarios import SCENARIOS, Scenario, scenario_by_name  # noqa: E402
from fixtures.workspace import write_fixture_workspace  # noqa: E402


# ---------------------------------------------------------------------------
# 回放专用的确定件
# ---------------------------------------------------------------------------


class _NeverCancelled(CancelToken):
    """永不取消。回放必须确定，所以取消信号是个常量。"""

    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return


@dataclass
class ScenarioReport:
    """一个场景的实测结果。字段与 ``Scenario`` 的期望值一一对应。"""

    name: str
    status: str
    rounds_used: int
    transcript_rounds: int
    remaining_rounds: int
    tool_calls: int
    tool_errors: int
    prompt_tokens: int
    completion_tokens: int
    wall_clock_ms: int
    text_preview: str
    transcript_fully_consumed: bool
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


# ---------------------------------------------------------------------------
# 工具表
# ---------------------------------------------------------------------------

TOOL_FACTORIES: dict[str, type] = {
    "read": ReadTool,
    "write": WriteTool,
    "edit": EditTool,
    "bash": BashTool,
    "grep": GrepTool,
}


def build_registry() -> ToolRegistry:
    """五个内置工具全注册。

    **不注册联网工具**：它们要 API key、要花额度，回放场景必须离线且免密。
    """
    registry = ToolRegistry()
    for tool in TOOL_FACTORIES.values():
        registry.register(tool())
    return registry


# ---------------------------------------------------------------------------
# 跑一个场景
# ---------------------------------------------------------------------------


async def run_scenario(scenario: Scenario, workspace: Path) -> ScenarioReport:
    """把场景喂进 loop，采集实测数据。

    ⚠️ 一个关键取舍：**loop 收到的是"历史"，不是 transcript**。

    transcript 是 **provider 的**输入（模型会说什么），
    而 ``run_turn`` 的 ``messages`` 参数是 **loop 的**输入（模型能看到什么）。
    两者完全独立——这正是"确定性回放"能成立的原因：
    把 provider 换成录音，loop 其余部分不变。

    所以这里构造的历史是**普通的两条用户消息**，不依赖 transcript 内容。
    """
    # 每个场景一份**新建的**工作区副本。
    # 不复用同一个目录：bash 工具能建目录能写文件，共用会让
    # "场景 A 建了 demo/out → 场景 B 恰好也能跑"这种顺序依赖混进来，
    # 而症状是"单跑红、全跑绿"。
    scenario_ws = write_fixture_workspace(workspace / scenario.name)

    provider = FakeProvider.from_file(scenario.path)

    # 历史：给 loop 一点"上文"，让 branch_and_resume 这类场景有意义。
    # 内容刻意与 transcript 无关——见上面 docstring 里的说明。
    history = [
        LlmMessageWrapper(
            timestamp=ts(1),
            message=UserMessage(content="先看看这个项目在做什么", timestamp=ts(1)),
        ),
    ]

    loop = AgentLoop(
        provider=provider,
        registry=build_registry(),
        model="fake-replay",
        session_id=f"eval-{scenario.name}",
        workspace_root=scenario_ws,
        # 轮数上限必须**大于**场景轮次：否则 max_rounds 会先把 loop 掐掉，
        # 症状是 status="stopped"，而看起来像"transcript 不够用"。
        max_rounds=scenario.rounds + 5,
        signal=_NeverCancelled(),
        clock=lambda: ts(1700000000),  # 固定时钟：报告要可复现
    )

    started = time.monotonic()
    result = await loop.run_turn(history)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # 统计工具调用与失败：遍历 loop 产出的 agent 层消息。
    # **不用 loop 内部的计数器**——那是实现细节；从产出反推是黑盒视角，
    # 换实现也不会让报告失真。
    #
    # ⚠️ 工具调用藏在**两层**里：assistant 消息被 ``LlmMessageWrapper`` 包着，
    # 真正的 ``ToolCallBlock`` 在 ``wrapper.message.content`` 上。
    # 只看 wrapper 的 ``content`` 会得到 0——而 0 看起来像"模型没调工具"，
    # 那是**假信号**（本次写运行器时就先踩了一次）。
    tool_calls = 0
    tool_errors = 0
    for message in result.messages:
        if type(message).__name__ == "ToolResultAgentMessage":
            tool_errors += 1 if getattr(message, "is_error", False) else 0
            continue
        tool_calls += _count_tool_calls(message)

    usage = result.usage
    report = ScenarioReport(
        name=scenario.name,
        status=result.status,
        rounds_used=result.rounds,
        transcript_rounds=scenario.rounds,
        remaining_rounds=provider.remaining_rounds,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        wall_clock_ms=elapsed_ms,
        text_preview=result.text[:120],
        transcript_fully_consumed=provider.remaining_rounds == 0,
    )

    report.mismatches = _check(scenario, report)
    return report


def _count_tool_calls(message: Any) -> int:
    """数一条 agent 层消息里的工具调用块。

    为什么要拆出这个函数：``ToolCallBlock`` 的**所在层数不固定**——
    assistant 消息被 ``LlmMessageWrapper`` 包了一层（块在 ``.message.content``），
    而工具结果消息的块直接在 ``.content``。写死一层就会数成 0，
    而 0 会被读成"模型一次工具都没调"，是个**看起来合理的假信号**。
    """
    blocks: list[Any] = []
    inner = getattr(message, "message", None)
    if inner is not None and hasattr(inner, "content"):
        blocks = list(inner.content or [])
    else:
        blocks = list(getattr(message, "content", None) or [])
    return sum(1 for block in blocks if type(block).__name__ == "ToolCallBlock")


def _check(scenario: Scenario, report: ScenarioReport) -> list[str]:
    """比对期望与实测，返回**全部**不一致处（不是遇到第一个就返回）。

    为什么收集全部：一次跑出所有偏差，比"修一个再跑一次"省事得多，
    而且能看出偏差之间是否有关联。
    """
    problems: list[str] = []
    if report.status != scenario.expected_status:
        problems.append(
            f"status: 期望 {scenario.expected_status}，实测 {report.status}"
        )
    if report.rounds_used != scenario.rounds:
        problems.append(f"轮数: 期望 {scenario.rounds}，实测 {report.rounds_used}")
    if report.tool_calls != scenario.expected_tool_calls:
        problems.append(
            f"工具调用数: 期望 {scenario.expected_tool_calls}，实测 {report.tool_calls}"
        )
    if report.tool_errors != scenario.expected_tool_errors:
        problems.append(
            f"工具失败数: 期望 {scenario.expected_tool_errors}，实测 {report.tool_errors}"
        )
    if not report.transcript_fully_consumed:
        problems.append(
            f"transcript 未被完整消费，剩 {report.remaining_rounds} 轮"
            "（通常意味着 loop 提前退出）"
        )
    return problems


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def render_markdown(reports: list[ScenarioReport], stamp: str) -> str:
    """产出 report.md 的正文。

    表格里的数字**全部来自实测**，不手写——手写的数字会随代码漂移
    （本项目已因此踩过一次：README 停在批次 6 的旧计数）。
    """
    lines = [
        "# 回放场景评测报告",
        "",
        f"- 生成时间戳（脚本注入，非挂钟）：{stamp}",
        f"- 场景数：{len(reports)}",
        f"- 通过：{sum(1 for r in reports if r.ok)} / {len(reports)}",
        "",
        "> 本报告由 `evals/runner.py` 生成。**不要手改**——",
        "> 手改的数字会与代码脱节，那正是这份报告要防的事。",
        "",
        "## 汇总",
        "",
        "| 场景 | 状态 | 轮数 | 工具调用 | 工具失败 | prompt tok | completion tok | 耗时 ms | 结果 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for report in reports:
        mark = "PASS" if report.ok else "FAIL"
        lines.append(
            f"| {report.name} | {report.status} | {report.rounds_used} | "
            f"{report.tool_calls} | {report.tool_errors} | {report.prompt_tokens} | "
            f"{report.completion_tokens} | {report.wall_clock_ms} | {mark} |"
        )

    failures = [r for r in reports if not r.ok]
    lines += ["", "## 偏差明细", ""]
    if not failures:
        lines.append("无。全部场景与清单里的期望值一致。")
    else:
        for report in failures:
            lines.append(f"### {report.name}")
            lines.append("")
            for problem in report.mismatches:
                lines.append(f"- {problem}")
            lines.append("")

    lines += [
        "",
        "## 怎么读这份报告",
        "",
        "1. **`prompt tok` 是整个 turn 的累计**（逐轮相加），不是最后一轮的。",
        "   这不是回放器的属性，是 `TurnResult.usage` 的口径——",
        "   它曾写成「只取最后一轮」，症状是「多轮任务更贵」这个事实在报告里消失，",
        "   而**不报任何错**。门槛 G58 现在钉住它（注入退回旧写法必须变红）。",
        "2. **轮数对不上是最有信息量的失败**：loop 提前退出（少）通常是"
        "「一失败就 return」这类分支混进来了；轮数多出来则是 transcript 被改过。",
        "3. **`transcript 完整消费` 是个独立信号**：状态全绿但没消费完，"
        "说明 loop 在有剩余输入时自己停了——那是个真 bug，不是噪声。",
        "4. **耗时（wall_clock_ms）在本机是噪声**：它主要反映的是"
        "**工具是否起了子进程**（`bash` 起 bash.exe ≈ 1–2 s，`read`/`grep` 纯文件 IO ≈ 个位数 ms），",
        "   不是 harness 的开销。真实性能基线见架构 7.3 节的 B0–B3。",
        "5. **本报告不含「任务成功率」类指标**——那需要 `evals/datasets/` 的任务集与判定脚本，",
        "   尚未落地。**不要拿本报告的 PASS 当成功率。**",
        "",
    ]
    return "\n".join(lines)


def write_reports(reports: list[ScenarioReport], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    json_path = out_dir / "replay_report.json"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "scenarios": [asdict(r) for r in reports],
                "passed": sum(1 for r in reports if r.ok),
                "total": len(reports),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    md_path = out_dir / "replay_report.md"
    md_path.write_text(render_markdown(reports, stamp), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def run_all(only: str | None, workspace_root: Path | None = None) -> list[ScenarioReport]:
    """跑选中的场景。

    ``workspace_root`` 给的是一份**临时**根目录（默认 ``tempfile``）——
    跑完即删。回放场景对被测项目**零副作用**：它只在自己的沙箱里写。
    """
    selected = [scenario_by_name(only)] if only is not None else list(SCENARIOS)

    owns_temp = workspace_root is None
    root = workspace_root or Path(tempfile.mkdtemp(prefix="sigma-eval-"))
    root.mkdir(parents=True, exist_ok=True)

    reports: list[ScenarioReport] = []
    try:
        for scenario in selected:
            try:
                report = await run_scenario(scenario, root)
            except Exception as exc:  # 场景自身崩了也要进报告，而不是让整轮评测中断
                report = ScenarioReport(
                    name=scenario.name,
                    status="<exception>",
                    rounds_used=0,
                    transcript_rounds=scenario.rounds,
                    remaining_rounds=-1,
                    tool_calls=0,
                    tool_errors=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    wall_clock_ms=0,
                    text_preview="",
                    transcript_fully_consumed=False,
                    mismatches=[f"{type(exc).__name__}: {exc}"],
                )
            reports.append(report)
            mark = "PASS" if report.ok else "FAIL"
            print(f"[{mark}] {scenario.name}")
            for problem in report.mismatches:
                print(f"       → {problem}")
    finally:
        if owns_temp:
            shutil.rmtree(root, ignore_errors=True)
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回放场景评测运行器")
    parser.add_argument(
        "--scenario",
        default=None,
        help="只跑一个场景（名字见 tests/fixtures/transcripts/_scenarios.py）",
    )
    parser.add_argument(
        "--report-dir",
        default=None,
        help="报告输出目录，默认 evals/reports/",
    )
    args = parser.parse_args(argv)

    reports = asyncio.run(run_all(args.scenario))
    out_dir = Path(args.report_dir) if args.report_dir else REPO / "evals" / "reports"
    json_path, md_path = write_reports(reports, out_dir)

    passed = sum(1 for r in reports if r.ok)
    print()
    print(f"{passed}/{len(reports)} 个场景通过")
    print(f"报告：{json_path.relative_to(REPO)}")
    print(f"      {md_path.relative_to(REPO)}")
    return 0 if passed == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
