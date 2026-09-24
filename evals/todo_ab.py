"""todo A/B 对照评测驱动：编排层主 agent 开 sub_agent 派两个子 agent 并行跑两臂。

两臂（星辰 2026-09-23 拍板）::

    A 臂（有 todo）   evals/task_runner.py --task syn-00N --profile B2
    B 臂（无 todo）   evals/task_runner.py --task syn-00N --profile B2 --no-todo

为什么编排交给真 sigma 主 agent，而不是脚本把命令顺序跑一遍
    这是 task 工具的第一次实战：并发 2、信箱回报（含子会话 token 用量，D-A1）、
    收尾兜底（G86 路径）、主子写批次互斥——脚本顺序跑只能测 task_runner 本身，
    测不到 harness 的编排能力。评测报告与工具实战同一次跑里同时产生。

主 agent 的编排流程（预期，不靠脚本硬编码）
    todo create 建两臂计划 → task dispatch ×2 → 没有其他事可做即收尾
    → loop 收尾兜底等两臂跑完（G86 路径）→ 回报注入（轮顶部 drain）
    → 读 8 份报告汇总对照表输出。

费用与时长预估（4 条任务，deepseek-chat）
    task_runner 8 次 ≈ 8 × 42k token；编排层 ≈ 60k token。两臂并行，总时长
    ≈ 慢的那条臂（4 条 × 60–90s）≈ 5–8 分钟。

用法::

    set -a; . ~/.sigma/.env; set +a
    ./.venv/Scripts/python.exe evals/todo_ab.py
    ./.venv/Scripts/python.exe evals/todo_ab.py --tasks syn-001 syn-002
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

EVALS = Path(__file__).resolve().parent
REPO = EVALS.parent
sys.path.insert(0, str(REPO / "core"))

from sigma import sdk  # noqa: E402
from sigma.cli import DEFAULT_PRESET, resolve_api_key  # noqa: E402
from sigma_ai.openai import OpenAICompatProvider  # noqa: E402
from sigma_ai.registry import builtin_providers  # noqa: E402

ORCH_SESSION_ID = "todo-ab-orch"

#: 默认先跑 4 条（任务 #22 的「8 次对照」口径）；报告出来后可再补 005–010。
DEFAULT_TASKS = ["syn-001", "syn-002", "syn-003", "syn-004"]


def _orchestrator_task(tasks: list[str]) -> str:
    """给编排主 agent 的任务描述。**必须自包含**——它看不到这段对话。"""
    arm_a = "\n".join(
        f"  ./.venv/Scripts/python.exe evals/task_runner.py --task {t} --profile B2"
        for t in tasks
    )
    arm_b = "\n".join(
        f"  ./.venv/Scripts/python.exe evals/task_runner.py --task {t} --profile B2 --no-todo"
        for t in tasks
    )
    return f"""你的任务：编排一次 todo 工具 A/B 对照评测（在当前仓库根目录执行）。不要自己执行评测命令——用 task 工具派两个子任务并行完成。

先 todo create 建两条计划（A 臂 / B 臂），然后 task dispatch 两个子任务：

子任务 1（A 臂，有 todo）：依次执行这 {len(tasks)} 条命令（每条命令约 1–2 分钟，bash 记得设 timeout=900）：
{arm_a}

子任务 2（B 臂，无 todo）：依次执行这 {len(tasks)} 条命令（同样 timeout=900）：
{arm_b}

两个子任务的回报里会有每条命令的退出码与指标。两个子任务都完成并回报后：
1. todo update 把两条计划标记 completed；
2. 读取 {len(tasks) * 2} 份报告 JSON（evals/reports/task_<id>_B2.json 与 task_<id>_B2-no-todo.json，<id> ∈ {tasks}）；
3. 汇总成一张 Markdown 对照表：每行一条任务、两列臂（B2 / B2-no-todo），单元格给：判定通过与否、轮数、工具调用次数、prompt+completion token、耗时秒；表尾附两臂的合计 token 与通过数对比。
对照表作为你的最终总结完整输出（不要省略行）。"""


async def _run(tasks: list[str], model: str) -> int:
    api_key, source = resolve_api_key(explicit=None)
    if not api_key:
        print("[错误] 没找到 API key（SIGMA_API_KEY / ~/.sigma/.env）", file=sys.stderr)
        return 2

    provider = OpenAICompatProvider(
        base_url=builtin_providers().resolve(DEFAULT_PRESET).base_url,
        api_key=api_key,
        provider_name=DEFAULT_PRESET,
    )
    # 提示词与注册表同源（批次 7 教训）：主会话开 sub_agent，提示词必须带
    # task 工具行——CLI 的 --sub-agent 也是这么拼的（test_task_line_follows_flag）。
    session = sdk.InteractiveSession(
        provider=provider,
        workspace_root=REPO,
        model=model,
        session_id=ORCH_SESSION_ID,
        system_prompt=sdk.build_system_prompt(task=True),
        emit=lambda line: print(f"[编排] {line}", flush=True),
        enable_sub_agent=True,
    )
    started = time.monotonic()
    try:
        result = await session.send(_orchestrator_task(tasks))
    finally:
        await provider.aclose()
    elapsed = time.monotonic() - started

    usage = result.usage
    print()
    print("=" * 78)
    print(f"编排会话结束：status={result.status} 轮数={result.rounds} "
          f"token={usage.prompt_tokens if usage else 0}+{usage.completion_tokens if usage else 0} "
          f"耗时 {elapsed:.0f}s")
    print("（子会话的 token 用量在各臂回报文本里——D-A1）")
    print("=" * 78)
    print()
    print(result.text)
    return 0 if result.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="todo A/B 对照评测（编排层主 agent 派工）")
    parser.add_argument(
        "--tasks", nargs="+", default=DEFAULT_TASKS,
        help=f"任务 id 列表（默认 {' '.join(DEFAULT_TASKS)}）",
    )
    parser.add_argument("--model", default=None, help="覆盖模型名（默认 deepseek-chat）")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.tasks, args.model or "deepseek-chat"))


if __name__ == "__main__":
    sys.exit(main())
