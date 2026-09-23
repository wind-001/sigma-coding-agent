"""任务集评测运行器：拿**真实模型**跑 ``evals/datasets/`` 下的任务，产出报告。

**与 runner.py 的分工（两件事，不合并）**

    runner.py        回放场景：FakeProvider、免 key、确定性、进 CI      ← 「测量仪」
    task_runner.py   真实任务集：真 provider、要 key、花钱、人工触发     ← 「被测对象」

    合并的代价是让回放运行器背上"要 key 才能跑"的负担，而它**能在 CI 里免费跑**
    正是它的价值所在。共用的是判定（``judges.py``）与数据（``task_types.py``）。

**档位（``EvalProfile``，P2-6 详规 §4.2 / §11.5）**

    B0  无工具、单轮：模型只凭任务描述一次性给出修好的文件，由运行器替它写入。
        这是**健全性检查**——它若能过，说明任务不需要 agent，整套评测白做。
    B1  有工具、满上下文、**无**压缩、无 checkpoint（对照组，显式关断）。
    B2  完整 harness（主结果档）。

    档位名进**报告文件名**（``task_<id>_<档位>.json``）与**每一行数据**——
    没有档位名，两个月后没人说得清那份 JSON 是在什么配置下产生的。

**为什么任务必须在临时目录里跑**

    agent 会改文件，判定还会用原始测试覆盖 ``tests/``。直接在 ``datasets/`` 上跑
    会**永久污染数据集**：跑完一次，那条任务的初始状态就没了，
    ``initial_failure`` 那份证据也跟着失效——而它正是 B 类任务合法性的全部依据。

用法
    ./.venv/Scripts/python.exe evals/task_runner.py --task syn-001 --profile B2
    ./.venv/Scripts/python.exe evals/task_runner.py --task syn-001 --check-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

EVALS = Path(__file__).resolve().parent
REPO = EVALS.parent
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(EVALS))

from judges import Judge, JudgeVerdict, PytestJudge  # noqa: E402
from task_types import TaskResult, TaskSpec  # noqa: E402
from runner import _count_tool_calls  # noqa: E402

from sigma.eval_profile import EvalProfile  # noqa: E402
from sigma import sdk  # noqa: E402
from sigma.cli import DEFAULT_PRESET, resolve_api_key  # noqa: E402
from sigma_agent.types import TurnResult  # noqa: E402
from sigma_ai.base import NeverCancelled, StreamOptions  # noqa: E402
from sigma_ai.messages import LlmMessage, SystemMessage, Usage, UserMessage  # noqa: E402
from sigma_ai.openai import OpenAICompatProvider  # noqa: E402
from sigma_ai.registry import builtin_providers  # noqa: E402
from sigma_ai.stamps import now as _stamp_now  # noqa: E402

DATASETS = EVALS / "datasets"
REPORTS = EVALS / "reports"

#: B1/B2 → 档位对象。B0 走的是单轮无工具的独立通路（``_run_b0``）。
_PROFILES: dict[str, type[EvalProfile]] = {"B1": EvalProfile.b1, "B2": EvalProfile.b2}


def _load_spec(task_dir: Path) -> TaskSpec:
    """读 ``meta.json``。字段缺失在**这一刻**就报错，不拖到报告里出现空白。"""
    return TaskSpec.model_validate_json((task_dir / "meta.json").read_text("utf-8"))


def _stage(task_dir: Path, spec: TaskSpec, into: Path) -> Path:
    """把任务的 workspace 拷到临时目录，返回工作区路径。

    **不拷 ``judge_tests/``**：那是判定的原始副本，agent 不该看到它。
    """
    target = into / "workspace"
    shutil.copytree(task_dir / spec.workspace, target)
    return target


def _judge_for(task_dir: Path, spec: TaskSpec) -> PytestJudge:
    restore = None
    if spec.judge_tests is not None:
        # source 是**任务目录**下的原始副本（绝对）；target 是**相对工作区**的路径——
        # 判定发生在临时副本上，覆盖必须盖到被测的那份（judges.PytestJudge 的 docstring
        # 记了第一版盖错地方、防篡改静默失效的事故）。
        restore = (task_dir / spec.judge_tests.source, spec.judge_tests.target)
    return PytestJudge(spec.judge.command, cwd=spec.judge.cwd, restore_tests=restore)


def _tool_failures(result: TurnResult) -> int:
    """工具结果里 ``is_error`` 的条数。**口径与 runner.py 的 ``tool_errors`` 一致。**"""
    return sum(
        1
        for message in result.messages
        if type(message).__name__ == "ToolResultAgentMessage"
        and getattr(message, "is_error", False)
    )


def _make_provider(api_key: str) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=builtin_providers().resolve(DEFAULT_PRESET).base_url,
        api_key=api_key,
        provider_name=DEFAULT_PRESET,
    )


async def _run_agent(
    spec: TaskSpec,
    workspace: Path,
    *,
    api_key: str,
    model: str,
    profile: EvalProfile,
) -> TurnResult:
    """把任务描述交给 sigma，返回它这一轮的结果。

    档位在这里落成 ``run_task`` 的两个开关：**B1 = 显式关断压缩与
    checkpoint**——不是"不传参数"（那样会被默认 32k 策略顶掉，
    详规 §11.5 第 1 条）。
    """
    provider = _make_provider(api_key)
    try:
        return await sdk.run_task(
            spec.description,
            provider=provider,
            workspace_root=workspace,
            model=model,
            project_instructions="",
            enable_compaction=profile.compaction,
            enable_checkpoint=profile.checkpoint,
        )
    finally:
        await provider.aclose()


async def _single_shot(
    provider: OpenAICompatProvider, messages: list[LlmMessage], model: str
) -> tuple[str, Usage | None]:
    """B0 的执行体：一次补全，收齐文本与用量。**没有任何工具参与。**"""
    parts: list[str] = []
    usage: Usage | None = None
    async for event in provider.stream(
        messages,
        tools=[],
        model=model,
        signal=NeverCancelled(),
        options=StreamOptions(include_usage=True),
    ):
        if event.type == "text_delta":
            parts.append(event.text)
        elif event.type == "usage":
            usage = event.usage
        elif event.type == "error":
            raise RuntimeError(f"provider 报错：{event}")
    return "".join(parts), usage


def _extract_code(text: str) -> str | None:
    """从回答里取第一个 ```python 代码块。

    取**第一个**而不是最后一个：模型常见格式是"一两句说明 + 代码"，
    说明里偶尔会有行内代码示例，但成块的文件通常只有一个。
    提不出来就如实报错——**猜一个写进工作区比报错危险得多**。
    """
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    return blocks[0].strip() if blocks else None


def _task_module(workspace: Path) -> Path:
    """B0 要替换的目标文件：工作区根下**唯一的**那个 .py 模块。

    找不到恰好一个就报错——B0 的前提是"单文件修复"，多于一个说明
    这条任务不适合 B0 口径，应该显式处理而不是挑一个碰运气。
    """
    modules = [p for p in workspace.glob("*.py") if p.is_file()]
    if len(modules) != 1:
        raise RuntimeError(
            f"B0 要求工作区根下恰好一个 .py 模块，找到 {len(modules)} 个："
            f"{[p.name for p in modules]}"
        )
    return modules[0]


async def _run_b0(
    spec: TaskSpec, workspace: Path, *, api_key: str, model: str
) -> TaskResult:
    """B0：无工具、单轮。模型凭任务描述一次给出修好的文件，运行器替它写入。

    **这是健全性检查，不是正式档**：它若通过，说明这条任务不需要 agent
    ——描述+源码就够，整套 agent 评测对这条任务没有区分度。
    """
    provider = _make_provider(api_key)
    started = time.monotonic()
    try:
        module = _task_module(workspace)
        source = module.read_text("utf-8")
        prompt = (
            "下面是一份有 bug 的 Python 模块，以及它的修复任务描述。\n\n"
            f"## 任务描述\n\n{spec.description}\n\n"
            f"## 当前源码（{module.name}）\n\n"
            f"```python\n{source}\n```\n\n"
            f"请只输出修好后的**完整** {module.name} 文件内容，"
            "放在一个 ```python 代码块里。不要输出任何其他文字。"
        )
        messages: list[LlmMessage] = [
            SystemMessage(
                content="你是严谨的工程师。只输出被要求的完整代码文件，不要输出别的。",
                timestamp=_stamp_now(),
            ),
            UserMessage(content=prompt, timestamp=_stamp_now()),
        ]
        text, usage = await _single_shot(provider, messages, model)
        code = _extract_code(text)
        elapsed = time.monotonic() - started
        if code is None:
            return TaskResult(
                task_id=spec.id,
                eval_profile="B0",
                status="error",
                rounds=1,
                tool_calls=0,
                tool_failures=0,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                wall_clock_s=round(elapsed, 3),
                error="B0 输出里没有可提取的代码块",
            )
        module.write_text(code + ("\n" if not code.endswith("\n") else ""), "utf-8")
        return TaskResult(
            task_id=spec.id,
            eval_profile="B0",
            status="completed",
            rounds=1,
            tool_calls=0,
            tool_failures=0,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            wall_clock_s=round(elapsed, 3),
        )
    except Exception as exc:  # noqa: BLE001 — 评测要如实记下失败
        elapsed = time.monotonic() - started
        return TaskResult(
            task_id=spec.id,
            eval_profile="B0",
            status="error",
            rounds=1,
            tool_calls=0,
            tool_failures=0,
            prompt_tokens=0,
            completion_tokens=0,
            wall_clock_s=round(elapsed, 3),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        await provider.aclose()


async def run_one(
    task_id: str, *, check_only: bool, model: str | None, profile_name: str
) -> int:
    task_dir = DATASETS / "synthetic" / task_id
    if not task_dir.is_dir():
        print(f"[错误] 找不到任务目录：{task_dir}", file=sys.stderr)
        return 2

    spec = _load_spec(task_dir)
    judge: Judge = _judge_for(task_dir, spec)
    print(f"任务 {spec.id}：{spec.title}")
    print(f"  档位     {profile_name}")
    print(f"  初始失败记录：{spec.initial_failure.summary}")
    print()

    with tempfile.TemporaryDirectory(prefix="sigma-eval-") as tmp:
        workspace = _stage(task_dir, spec, Path(tmp))

        if check_only:
            verdict = judge.judge(workspace, None)  # type: ignore[arg-type]
            return _report_check_only(spec, verdict, profile_name)

        api_key, source = resolve_api_key(explicit=None)
        if not api_key:
            print("[错误] 没找到 API key（SIGMA_API_KEY / ~/.sigma/.env）", file=sys.stderr)
            return 2
        print(f"  模型     {model or 'deepseek-chat'}（key 来源：{source}）")

        started = time.monotonic()
        if profile_name == "B0":
            task_result = await _run_b0(
                spec, workspace, api_key=api_key, model=model or "deepseek-chat"
            )
            verdict = judge.judge(workspace, task_result)
            return _report(spec, task_result, verdict, profile_name)

        profile = _PROFILES[profile_name]()
        try:
            result = await _run_agent(
                spec,
                workspace,
                api_key=api_key,
                model=model or "deepseek-chat",
                profile=profile,
            )
        except Exception as exc:  # noqa: BLE001 — 评测要如实记下失败，不能让它炸掉整轮
            elapsed = time.monotonic() - started
            print(f"[异常] agent 这一轮抛了：{type(exc).__name__}: {exc}")
            task_result = TaskResult(
                task_id=spec.id,
                eval_profile=profile.name,
                status="error",
                rounds=0,
                tool_calls=0,
                tool_failures=0,
                prompt_tokens=0,
                completion_tokens=0,
                wall_clock_s=round(elapsed, 3),
                error=f"{type(exc).__name__}: {exc}",
            )
            verdict = JudgeVerdict(passed=False, reason="agent 抛异常", evidence={})
            return _report(spec, task_result, verdict, profile_name)

        elapsed = time.monotonic() - started
        usage = result.usage
        task_result = TaskResult(
            task_id=spec.id,
            eval_profile=profile.name,
            status=result.status,
            rounds=result.rounds,
            tool_calls=sum(_count_tool_calls(m) for m in result.messages),
            tool_failures=_tool_failures(result),
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            wall_clock_s=round(elapsed, 3),
        )
        print(f"  agent    status={task_result.status} 轮数={task_result.rounds} "
              f"工具={task_result.tool_calls}（失败 {task_result.tool_failures}）"
              f"token={task_result.prompt_tokens}+{task_result.completion_tokens} "
              f"{task_result.wall_clock_s}s")

        verdict = judge.judge(workspace, task_result)
        return _report(spec, task_result, verdict, profile_name)


def _report_check_only(spec: TaskSpec, verdict: JudgeVerdict, profile_name: str) -> int:
    """``--check-only``：只验"这条任务在初始状态下确实失败"。

    **这是 B 类任务合法性的唯一依据**（详规 §2.2）：判定命令此时必须**非 0**。
    如果它已经能过，说明这条任务是"先写实现再补测试"造出来的，应当作废。

    ⚠️ **"非 0"不等于"失败"——还有第三种情况：命令压根没跑起来。**

        第一版这里只看 ``verdict.passed``，于是 ``exit_code=None``
        （命令找不到解释器）也被报成"初始状态确实失败 ✓"。
        那是**假绿**：复验看起来通过了，而它什么都没验证。

        所以先分三态：没跑起来（无效）／超时（无效）／真的非 0（有效）。
        顺序不能反——**"无效"必须先于"通过"被判定**。
    """
    exit_code = verdict.evidence.get("exit_code")
    print("── 初始状态复验 ──")
    print(f"  判定 exit_code = {exit_code!r}（meta.json 记录的是 {spec.initial_failure.exit_code}）")

    if verdict.evidence.get("invalid"):
        print(f"\n[无效] 判定没跑起来：{verdict.evidence['invalid']}")
        print('       —— 这**不是**"初始状态失败"的证据，复验作废。')
        return 2
    if verdict.evidence.get("os_error"):
        print(f"\n[无效] 判定命令没跑起来：{verdict.evidence['os_error']}")
        print('       —— 这**不是**"初始状态失败"的证据，复验作废。')
        return 2
    if verdict.evidence.get("timeout_seconds"):
        print("\n[无效] 判定命令超时，复验作废。")
        return 2

    summary = str(verdict.evidence.get("summary", ""))
    if summary:
        print(f"  摘要：{summary.splitlines()[-1]}")

    if verdict.passed:
        print("\n[失败] 判定命令在**初始状态**就通过了 —— 这条任务不是 B 类任务，应当作废。")
        return 1
    print("\n[通过] 初始状态确实失败 ✓（B 类任务的硬性前提成立）")
    return 0


def _report(
    spec: TaskSpec, result: TaskResult, verdict: JudgeVerdict, profile_name: str
) -> int:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    REPORTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": stamp,
        "task": spec.id,
        "title": spec.title,
        "kind": spec.kind,
        "result": result.model_dump(),
        "verdict": verdict.model_dump(),
    }
    json_path = REPORTS / f"task_{spec.id}_{profile_name}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    md = [
        f"# 任务评测：{spec.id} —— {spec.title}（档位 {profile_name}）",
        "",
        f"> 生成时间 {stamp} ｜ 判定器 `PytestJudge` ｜ 档位 **{result.eval_profile}**",
        "",
        "## 判定",
        "",
        f"- **{'通过' if verdict.passed else '未通过'}** —— {verdict.reason}",
        "",
        "## 指标（口径与 runner.py 对齐）",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| status | `{result.status}` |",
        f"| 轮数 | {result.rounds} |",
        f"| 工具调用 | {result.tool_calls} |",
        f"| 工具失败 | {result.tool_failures} |",
        f"| prompt token | {result.prompt_tokens} |",
        f"| completion token | {result.completion_tokens} |",
        f"| wall-clock | {result.wall_clock_s}s |",
        "",
        "## 证据（为什么这么判）",
        "",
        "```json",
        json.dumps(verdict.evidence, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    md_path = REPORTS / f"task_{spec.id}_{profile_name}.md"
    md_path.write_text("\n".join(md), "utf-8")

    print()
    print(f"  判定     {'通过' if verdict.passed else '未通过'} —— {verdict.reason}")
    if not verdict.passed:
        print(f"  证据     {verdict.evidence.get('summary', '')}")
    print(f"  报告     {json_path.relative_to(REPO)} / {md_path.relative_to(REPO)}")
    return 0 if verdict.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑一条任务集的评测")
    parser.add_argument("--task", required=True, help="任务 id（datasets/synthetic/<id>）")
    parser.add_argument("--model", default=None, help="覆盖模型名")
    parser.add_argument(
        "--profile",
        default="B2",
        choices=["B0", "B1", "B2"],
        help="评测档位：B0=无工具单轮（健全性）/ B1=无干预对照 / B2=完整 harness",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只复验『初始状态确实失败』，不调模型、不花钱",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run_one(args.task, check_only=args.check_only, model=args.model, profile_name=args.profile)
    )


if __name__ == "__main__":
    sys.exit(main())
