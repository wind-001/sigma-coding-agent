"""真实 API 冒烟探测：把 P1 批次 1 §9 里"待真实 API 复核"的项提前兑现。

这个脚本的目的**不是**"聊一句看看能不能通"。

``core/sigma_ai/openai_compat.py`` 的 docstring 结尾写着：

    > 批次 1 无法验证的项（详规第 9 节 R2–R6）
    > 错误码映射的真实性 / SSE 分帧的健壮性 / ``finish_reason`` 的实际取值 /
    > ``usage`` 是否真能取到 / 多工具 index 归属。这些要批次 4 接真实 API 复核。

本脚本就是去复核这五项。**它依赖的只有 ``sigma_ai`` 一层**——
不需要 agent loop、不需要工具系统、不需要会话树（那三样都还没实现）。
``OpenAICompatProvider.stream()`` 本身就是一个可直接消费的异步生成器。

五项探测与对应关系
    P1 连通与流式          → 基本可用性
    P2 用量可测性          → **R5**：``stream_options.include_usage`` 是否真生效
    P3 截断时的停止原因    → **R4**：``finish_reason`` 实际返回什么取值
    P4 工具调用分片归属    → **R6**：``tool_calls[].index`` 是否可靠
    P5 工具结果多轮回传    → **R2 的一部分**：``role="tool"`` 消息是否被接受

每个探测都打印**耗时与 token 用量**——没有数字的结论不算结论。

用法::

    export SIGMA_API_KEY=sk-...
    .venv/Scripts/python.exe scripts/real_api_smoke.py

    或显式传参：
    .venv/Scripts/python.exe scripts/real_api_smoke.py \\
        --base-url https://api.deepseek.com/v1 --model deepseek-chat

**API key 不落盘、不打印、不进日志。** 只从环境变量或命令行读。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import warnings
from typing import Any

# Windows 控制台默认可能是 GBK，中文输出会炸。必须在任何输出前设置。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from sigma_ai.base import CancelToken, SamplingParams, StreamOptions
from sigma_ai.events import (
    ErrorEvent,
    StopEvent,
    TextDelta,
    ToolCallDelta,
    UsageEvent,
)
from sigma_ai.messages import (
    AssistantMessage,
    LlmMessage,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from sigma_ai.openai_compat import OpenAICompatProvider, UnmappedFinishReason

# ---------------------------------------------------------------------------
# 常用厂商预设。--preset 选一个即可，省得每次敲 base_url。
# ---------------------------------------------------------------------------

PRESETS: dict[str, tuple[str, str]] = {
    # 名称: (base_url, 默认 model)
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5:7b"),
}

DEFAULT_PRESET = "deepseek"


class _NeverCancelled(CancelToken):
    """永不取消。真实取消语义的验证不在本脚本范围（那是批次 4 的 R1）。"""

    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 一轮调用的观测结果
# ---------------------------------------------------------------------------


class Turn:
    """收集一轮 ``stream()`` 的全部可观测输出。"""

    def __init__(self) -> None:
        self.text: str = ""
        self.text_delta_count: int = 0
        self.text_signatures: list[str | None] = []
        # index -> {"id":..., "name":..., "arguments":...}
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.tool_delta_count: int = 0
        self.usage: Usage | None = None
        self.stop_reason: str | None = None
        self.errors: list[str] = []
        self.event_count: int = 0
        self.unmapped_finish_reasons: list[str] = []
        self.elapsed_s: float = 0.0

    def events_summary(self) -> str:
        """事件类型计数的紧凑表示，用于验证事件序列是否符合预期。"""
        parts = [f"text_delta×{self.text_delta_count}"]
        if self.tool_delta_count:
            parts.append(f"tool_call_delta×{self.tool_delta_count}")
        if self.usage is not None:
            parts.append("usage×1")
        if self.stop_reason is not None:
            parts.append("stop×1")
        if self.errors:
            parts.append(f"error×{len(self.errors)}")
        return " + ".join(parts)

    def assembled_tool_arguments(self) -> dict[int, str]:
        """index -> 拼好的 arguments 字符串。验证分片是否按 index 正确归属。"""
        return {
            index: str(slot.get("arguments") or "")
            for index, slot in sorted(self.tool_calls.items())
        }


async def run_turn(
    provider: OpenAICompatProvider,
    messages: list[LlmMessage],
    tools: list[dict[str, Any]],
    *,
    model: str,
    sampling: SamplingParams | None = None,
    options: StreamOptions | None = None,
    timeout_s: float = 60.0,
) -> Turn:
    """跑一轮并把事件收进 :class:`Turn`。

    用 ``warnings.catch_warnings(record=True)`` 把 provider 内部的告警
    **捕获并带出来**，而不是让它们散落在 stderr 里。
    ``UnmappedFinishReason`` 是关键信号：它说明真实 API 返回了我们表里没有的
    ``finish_reason``——那正是 R4 要查的东西。
    """
    turn = Turn()
    started = time.perf_counter()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        async for event in provider.stream(
            messages,
            tools,
            model=model,
            signal=_NeverCancelled(),
            sampling=sampling,
            options=options,
            timeout_s=timeout_s,
        ):
            turn.event_count += 1

            if isinstance(event, TextDelta):
                turn.text += event.text
                turn.text_delta_count += 1
                turn.text_signatures.append(event.text_signature)

            elif isinstance(event, ToolCallDelta):
                turn.tool_delta_count += 1
                slot = turn.tool_calls.setdefault(
                    event.index, {"id": None, "name": None, "arguments": ""}
                )
                if event.id:
                    slot["id"] = event.id
                if event.name:
                    slot["name"] = event.name
                slot["arguments"] += event.arguments_delta

            elif isinstance(event, UsageEvent):
                turn.usage = event.usage

            elif isinstance(event, StopEvent):
                turn.stop_reason = event.stop_reason

            elif isinstance(event, ErrorEvent):
                turn.errors.append(f"{event.error.code}: {event.error.message}")

    turn.elapsed_s = time.perf_counter() - started
    turn.unmapped_finish_reasons = [
        str(w.message)
        for w in caught
        if issubclass(w.category, UnmappedFinishReason)
    ]
    return turn


# ---------------------------------------------------------------------------
# 输出helper
# ---------------------------------------------------------------------------


def head(title: str) -> None:
    print()
    print("=" * 74)
    print(f"  {title}")
    print("=" * 74)


def row(label: str, value: Any) -> None:
    print(f"  {label:<22} {value}")


def verdict(ok: bool, text: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {text}")


def sample(text: str, limit: int = 160) -> str:
    """截断长文本用于展示，保留真实换行信息。"""
    flat = text.replace("\n", "\\n")
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…（共 {len(text)} 字符）"


# ---------------------------------------------------------------------------
# 五项探测
# ---------------------------------------------------------------------------


async def probe_p1_liveness(
    provider: OpenAICompatProvider, model: str
) -> tuple[Turn, bool]:
    """P1：最简连通 + 流式文本。"""
    head("P1 连通与流式（基本可用性）")
    turn = await run_turn(
        provider,
        [UserMessage(content="用一句话回答：1+1 等于几？", timestamp=int(time.time()))],
        [],
        model=model,
        sampling=SamplingParams(max_tokens=64, temperature=0.0),
    )
    row("耗时", f"{turn.elapsed_s:.2f} s")
    row("事件总数", turn.event_count)
    row("事件序列", turn.events_summary())
    row("返回文本", sample(turn.text))
    row("stop_reason", turn.stop_reason)
    if turn.usage:
        row(
            "token 用量",
            f"prompt={turn.usage.prompt_tokens} "
            f"completion={turn.usage.completion_tokens}",
        )
    for err in turn.errors:
        row("错误", err)

    ok = bool(turn.text) and turn.stop_reason is not None and not turn.errors
    verdict(ok, "拿到流式文本且正常收尾" if ok else "未拿到完整响应")
    # 顺带记录分帧是否可疑：文本极短但 delta 极多，说明切得很碎（不算错）。
    if turn.text and turn.text_delta_count:
        ratio = len(turn.text) / turn.text_delta_count
        row("平均每 delta 字符数", f"{ratio:.1f}（越小说明分帧越碎）")
    return turn, ok


async def probe_p2_usage(
    provider: OpenAICompatProvider, model: str
) -> tuple[Turn, bool]:
    """P2 / R5：``stream_options.include_usage`` 是否真的让 usage 回来。"""
    head("P2 用量可测性（R5：usage 是否真能取到）")
    turn = await run_turn(
        provider,
        [
            UserMessage(
                content="请连续写 20 个「好」字，不要有任何其他内容。",
                timestamp=int(time.time()),
            )
        ],
        [],
        model=model,
        sampling=SamplingParams(max_tokens=128, temperature=0.0),
        options=StreamOptions(include_usage=True),
    )
    row("耗时", f"{turn.elapsed_s:.2f} s")
    row("事件序列", turn.events_summary())
    row("返回文本", sample(turn.text))

    if turn.usage is None:
        row("usage", "**未返回**")
        verdict(
            False,
            "usage 没回来 —— 本地估算仍是唯一来源，评测报告不能用真实用量作证据",
        )
        return turn, False

    row(
        "usage",
        f"prompt={turn.usage.prompt_tokens} "
        f"completion={turn.usage.completion_tokens} "
        f"cached={turn.usage.cached_tokens}",
    )
    # 交叉验证：completion_tokens 应与实际产出字符数同量级。
    # 中文大致 1 字 ≈ 1 token（各家不同），若严重偏离则说明 usage 不可信。
    plausible = turn.usage.completion_tokens > 0 and turn.usage.prompt_tokens > 0
    if turn.text:
        approx = len(turn.text)
        row("产出字符数", f"{approx}（与 completion_tokens 同量级即合理）")
    row("cached_tokens", turn.usage.cached_tokens)
    verdict(plausible, "usage 可用，可作为账本" if plausible else "usage 数值可疑")
    return turn, plausible


async def probe_p3_finish_reason(
    provider: OpenAICompatProvider, model: str
) -> tuple[Turn, bool]:
    """P3 / R4:强制截断，看真实 ``finish_reason`` 取值。

    给一个极小的 ``max_tokens`` 并要求长输出，逼出 ``length`` 分支。
    若真实取值未被 ``_FINISH_REASON_MAP`` 收录，会触发 ``UnmappedFinishReason``
    告警——本探测会把它显示出来，这就是 R4 的答案。
    """
    head("P3 截断时的停止原因（R4：finish_reason 实际取值）")
    turn = await run_turn(
        provider,
        [
            UserMessage(
                content="请写一篇 500 字以上的文章，主题不限。",
                timestamp=int(time.time()),
            )
        ],
        [],
        model=model,
        sampling=SamplingParams(max_tokens=16, temperature=0.0),
    )
    row("耗时", f"{turn.elapsed_s:.2f} s")
    row("事件序列", turn.events_summary())
    row("返回文本", sample(turn.text))
    row("映射后 stop_reason", turn.stop_reason)
    if turn.unmapped_finish_reasons:
        for item in turn.unmapped_finish_reasons:
            row("未收录的 finish_reason", item)
    else:
        row("未收录的 finish_reason", "无（表已覆盖）")

    ok = turn.stop_reason in {"length", "stop"}
    if turn.stop_reason == "length":
        verdict(True, "截断被正确识别为 length（不是伪装成 stop）")
    elif turn.stop_reason == "stop":
        verdict(False, "截断却报 stop —— 评测里最危险的一类假通过，需查映射表")
    else:
        verdict(bool(turn.stop_reason), f"停止原因 = {turn.stop_reason}")
    return turn, ok


TOOL_READ = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取指定路径的文本文件内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件绝对路径",
                }
            },
            "required": ["path"],
        },
    },
}


async def probe_p4_tool_call(
    provider: OpenAICompatProvider, model: str
) -> tuple[Turn, bool]:
    """P4 / R6:工具调用分片是否按 ``index`` 正确归属。"""
    head("P4 工具调用分片归属（R6：tool_calls[].index 是否可靠）")
    turn = await run_turn(
        provider,
        [
            UserMessage(
                content=(
                    "请调用 read_file 工具读取 /tmp/sigma_demo.txt，"
                    "不要先问我，也不要解释。"
                ),
                timestamp=int(time.time()),
            )
        ],
        [TOOL_READ],
        model=model,
        sampling=SamplingParams(temperature=0.0),
    )
    row("耗时", f"{turn.elapsed_s:.2f} s")
    row("事件序列", turn.events_summary())
    row("stop_reason", turn.stop_reason)
    row("工具调用个数", len(turn.tool_calls))

    ok = False
    for index, slot in sorted(turn.tool_calls.items()):
        row(f"  [{index}] id", slot.get("id"))
        row(f"  [{index}] name", slot.get("name"))
        args = str(slot.get("arguments") or "")
        row(f"  [{index}] arguments", sample(args, 200))
        # 三个必要条件：有 id、有 name、arguments 是**合法 JSON**
        # ——最后一条正是"分片按 index 归属是否正确"的判据：
        # 归属错了会拼出两个半截 JSON。
        import json as _json

        try:
            parsed = _json.loads(args)
            row(f"  [{index}] JSON 解析", f"PASS → {parsed}")
            if slot.get("id") and slot.get("name"):
                ok = True
        except _json.JSONDecodeError as exc:
            row(f"  [{index}] JSON 解析", f"FAIL → {exc}")

    row("usage", turn.usage.model_dump() if turn.usage else "未返回")
    verdict(
        ok,
        "工具调用分片拼装完整且是合法 JSON"
        if ok
        else "分片拼装失败或模型没调用工具",
    )
    return turn, ok


async def probe_p5_tool_result_roundtrip(
    provider: OpenAICompatProvider,
    model: str,
    previous: Turn,
) -> tuple[Turn, bool]:
    """P5 / R2:把工具结果作为 ``role="tool"`` 消息回传，验证多轮是否成立。

    这是**唯一需要构造多轮上下文**的探测，也是最接近真实 agent loop 的部分。
    协议上工具结果必须紧跟在带 ``tool_calls`` 的 assistant 消息之后。
    """
    head("P5 工具结果多轮回传（R2：role=\"tool\" 消息是否被接受）")

    if not previous.tool_calls:
        row("跳过", "上一轮没有产生工具调用，本探测无输入")
        verdict(False, "跳过（缺前置的工具调用）")
        return Turn(), False

    # 取第一个工具调用构造回应
    index, slot = sorted(previous.tool_calls.items())[0]
    call_id = str(slot.get("id") or "")
    call_name = str(slot.get("name") or "")
    if not call_id or not call_name:
        row("跳过", f"工具调用缺少 id 或 name：id={call_id!r} name={call_name!r}")
        return Turn(), False

    now = int(time.time())
    assistant = AssistantMessage(
        content=[
            ToolCallBlock(
                id=call_id,
                name=call_name,
                arguments={"path": "/tmp/sigma_demo.txt"},
            )
        ],
        model=model,
        usage=previous.usage or Usage(prompt_tokens=0, completion_tokens=0),
        stop_reason="tool_use",
        timestamp=now,
    )
    tool_result = ToolResultMessage(
        tool_call_id=call_id,
        tool_name=call_name,
        content=[TextBlock(text="第一行：sigma 冒烟测试\n第二行：这是工具返回的伪造内容")],
        timestamp=now,
    )

    messages: list[LlmMessage] = [
        SystemMessage(content="你是一个简洁的助手。", timestamp=now),
        UserMessage(content="读取 /tmp/sigma_demo.txt", timestamp=now),
        assistant,
        tool_result,
    ]

    turn = await run_turn(
        provider,
        messages,
        [TOOL_READ],
        model=model,
        sampling=SamplingParams(max_tokens=128, temperature=0.0),
    )
    row("耗时", f"{turn.elapsed_s:.2f} s")
    row("事件序列", turn.events_summary())
    row("返回文本", sample(turn.text))
    row("stop_reason", turn.stop_reason)
    row("usage", turn.usage.model_dump() if turn.usage else "未返回")
    for err in turn.errors:
        row("错误", err)

    ok = bool(turn.text) and not turn.errors
    verdict(
        ok,
        "多轮上下文被接受，模型基于工具结果作答"
        if ok
        else "多轮回传失败——这通常会暴露 role/tool_call_id 的协议细节",
    )
    return turn, ok


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="sigma 真实 API 冒烟探测（不依赖 agent loop）"
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default=None,
        help=f"厂商预设，默认 {DEFAULT_PRESET}",
    )
    parser.add_argument("--base-url", default=None, help="覆盖 base_url（不含 /chat/completions）")
    parser.add_argument("--model", default=None, help="覆盖模型名")
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key；不传则读环境变量 SIGMA_API_KEY（推荐用环境变量）",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="只跑某个探测，取值 p1/p2/p3/p4/p5，逗号分隔",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="单次请求超时秒数，默认 60",
    )
    return parser


async def main_async(args: argparse.Namespace) -> int:
    preset_name = args.preset or DEFAULT_PRESET
    preset_url, preset_model = PRESETS[preset_name]

    base_url = args.base_url or os.environ.get("SIGMA_BASE_URL") or preset_url
    model = args.model or os.environ.get("SIGMA_MODEL") or preset_model
    api_key = args.api_key or os.environ.get("SIGMA_API_KEY")

    head("配置")
    row("preset", preset_name)
    row("base_url", base_url)
    row("model", model)
    row(
        "api_key",
        f"已提供（长度 {len(api_key)}，不打印内容）" if api_key else "**未提供**",
    )

    if not api_key:
        print()
        print("  缺少 API key。二选一：")
        print("    1) export SIGMA_API_KEY=sk-xxx   （推荐，不进 shell 历史）")
        print("    2) --api-key sk-xxx")
        print()
        print("  注意：本脚本不读 .env —— 若后续要读，.env 必须先加进 .gitignore。")
        return 2

    provider = OpenAICompatProvider(
        base_url=base_url, api_key=api_key, provider_name=preset_name
    )

    selected = (
        {s.strip().lower() for s in args.only.split(",") if s.strip()}
        if args.only
        else {"p1", "p2", "p3", "p4", "p5"}
    )

    results: list[tuple[str, bool]] = []
    p4_turn = Turn()

    try:
        if "p1" in selected:
            _, ok = await probe_p1_liveness(provider, model)
            results.append(("P1 连通与流式", ok))

        if "p2" in selected:
            _, ok = await probe_p2_usage(provider, model)
            results.append(("P2 用量可测（R5）", ok))

        if "p3" in selected:
            _, ok = await probe_p3_finish_reason(provider, model)
            results.append(("P3 截断停止原因（R4）", ok))

        if "p4" in selected:
            p4_turn, ok = await probe_p4_tool_call(provider, model)
            results.append(("P4 工具分片归属（R6）", ok))

        if "p5" in selected:
            if "p4" not in selected:
                # p5 依赖 p4 的产物，单独跑 p5 时先补一次 p4（不重复计入结果）
                p4_turn, _ = await probe_p4_tool_call(provider, model)
            _, ok = await probe_p5_tool_result_roundtrip(provider, model, p4_turn)
            results.append(("P5 工具结果回传（R2）", ok))
    finally:
        await provider.aclose()

    head("汇总")
    for name, ok in results:
        verdict(ok, name)
    passed = sum(1 for _, ok in results if ok)
    print()
    print(f"  {passed}/{len(results)} 项通过")
    print()
    print("  下一步：把本脚本的实测结论回填到")
    print("    docs/plans/P1-批次1-详规.md 第 9 节（R2–R6 由「待验证」改为实测结论）")
    print()

    return 0 if passed == len(results) else 1


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n  已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
