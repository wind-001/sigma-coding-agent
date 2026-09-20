"""真实 API 端到端 demo：让 agent loop 在真实模型上跑完一个任务。

与 `real_api_smoke.py` 的分工
    冒烟脚本验证的是 **Provider 通不通**（五项探测，不涉及 loop）。
    本脚本验证的是 **整条链能不能自己动手改文件**：
    任务描述 → 模型决定调工具 → 执行 → 结果回传 → 模型继续 → 完成。

    两者是递进关系：冒烟不通过，就别跑这个。

任务设计
    "把 input.txt 里的数字加 1 写进 output.txt" —— 它强制 **两次工具调用**
    （一次 read、一次 write），因而能验证：
    1. loop 真的循环了（不是一轮就结束）；
    2. 工具结果被回传给模型，模型基于它继续；
    3. 写工具真的落盘了。

    如果只让它"写一个文件"，一轮就结束，验证不到第 2 点。

用法::

    export SIGMA_API_KEY=sk-...
    .venv/Scripts/python.exe scripts/real_api_agent_demo.py

退出码：0 = 跑完（不管任务成没成，符合 Q4）；非 0 = harness 自身故障。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sigma_agent.agent_messages import (  # noqa: E402
    AgentMessage,
    LlmMessageWrapper,
    ToolResultAgentMessage,
)
from sigma_agent.loop import AgentLoop  # noqa: E402
from sigma_agent.registry import ToolRegistry  # noqa: E402
from sigma_ai.base import CancelToken, SamplingParams  # noqa: E402
from sigma_ai.messages import SystemMessage, UserMessage  # noqa: E402
from sigma_ai.openai_compat import OpenAICompatProvider  # noqa: E402
from sigma_tools.read import ReadTool  # noqa: E402
from sigma_tools.write import WriteTool  # noqa: E402

SYSTEM_PROMPT = """你是一个在本地工作区里干活的编程助手。

规则：
1. 用 read 工具读取文件，用 write 工具写入文件。
2. 一次只做一件必要的事，不要提前把多步操作合在一起。
3. 完成后用一句话说明你做了什么，不要输出多余的解释。
"""

TASK = """工作区里的 input.txt 中有一个整数。

请：读取它，把数字加 1，然后把结果写进 output.txt。
output.txt 的内容应当只有那个数字，不要有其它字符。"""


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


def _banner(title: str) -> None:
    print()
    print("=" * 74)
    print(f"  {title}")
    print("=" * 74)


def _describe(message: AgentMessage, index: int) -> str:
    """把一条产出消息渲染成一行可读的说明。"""
    if isinstance(message, LlmMessageWrapper):
        inner = message.message
        role = getattr(inner, "role", "?")
        blocks = getattr(inner, "content", [])
        parts: list[str] = []
        for block in blocks:
            kind = getattr(block, "type", "?")
            if kind == "text":
                parts.append(f"文本 {getattr(block, 'text', '')[:80]!r}")
            elif kind == "tool_call":
                parts.append(
                    f"调用 {getattr(block, 'name', '?')}"
                    f"({getattr(block, 'arguments', {})})"
                )
        return f"[{index}] assistant/{role}: " + ("; ".join(parts) or "(空)")

    if isinstance(message, ToolResultAgentMessage):
        text = ""
        for block in message.content:
            if getattr(block, "type", "") == "text":
                text = getattr(block, "text", "")[:100]
                break
        flag = "错误" if message.is_error else "结果"
        return f"[{index}] {flag} <- {message.tool_name}: {text!r}"

    return f"[{index}] {type(message).__name__}"


async def main() -> int:
    api_key = os.environ.get("SIGMA_API_KEY")
    if not api_key:
        print("缺少 SIGMA_API_KEY 环境变量。")
        print("用法：export SIGMA_API_KEY=sk-... && python scripts/real_api_agent_demo.py")
        return 2

    base_url = os.environ.get("SIGMA_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("SIGMA_MODEL", "deepseek-chat")

    workspace = Path(tempfile.mkdtemp(prefix="sigma-agent-demo-"))
    (workspace / "input.txt").write_text("41\n", encoding="utf-8")

    _banner("配置")
    print(f"  工作区      {workspace}")
    print(f"  base_url    {base_url}")
    print(f"  model       {model}")
    print(f"  api_key     已提供（长度 {len(api_key)}）")
    print(f"  input.txt   {(workspace / 'input.txt').read_text(encoding='utf-8').strip()!r}")

    registry = ToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    print(f"  已注册工具  {registry.names()}")

    provider = OpenAICompatProvider(
        base_url=base_url, api_key=api_key, provider_name="deepseek"
    )

    now = int(time.time())
    history: list[AgentMessage] = [
        LlmMessageWrapper(
            timestamp=now,
            message=SystemMessage(content=SYSTEM_PROMPT, timestamp=now),
        ),
        LlmMessageWrapper(
            timestamp=now,
            message=UserMessage(content=TASK, timestamp=now),
        ),
    ]

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        model=model,
        session_id="demo",
        workspace_root=workspace,
        max_rounds=8,
        sampling=SamplingParams(temperature=0.0),
        signal=_NeverCancelled(),
    )

    _banner("执行")
    started = time.perf_counter()
    try:
        result = await loop.run_turn(history)
    finally:
        await provider.aclose()
    elapsed = time.perf_counter() - started

    for index, message in enumerate(result.messages, start=1):
        print(f"  {_describe(message, index)}")

    _banner("结果")
    print(f"  状态        {result.status}")
    print(f"  轮数        {result.rounds}")
    print(f"  耗时        {elapsed:.2f} s")
    if result.usage is not None:
        print(
            f"  token       prompt={result.usage.prompt_tokens} "
            f"completion={result.usage.completion_tokens}"
        )
    if result.reason:
        print(f"  停止原因    {result.reason}")
    print(f"  最终文本    {result.text!r}")

    output = workspace / "output.txt"
    _banner("验收")
    if not output.exists():
        print("  [FAIL] output.txt 没有生成——模型没有调用 write，或调用失败。")
        print(f"  工作区保留在 {workspace} 供排查。")
        return 0  # Q4：任务没做成不算 harness 故障，退出码仍为 0

    produced = output.read_text(encoding="utf-8").strip()
    ok = produced == "42"
    print(f"  output.txt 内容  {produced!r}")
    print(f"  [{'PASS' if ok else 'FAIL'}] 期望 '42'")

    if ok:
        shutil.rmtree(workspace, ignore_errors=True)
        print("  （工作区已清理）")
    else:
        print(f"  工作区保留在 {workspace} 供排查。")

    print()
    print("  说明：这里验证的是「整条链能不能自己动手」，")
    print("  与 real_api_smoke.py 的「Provider 通不通」是两件事。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
