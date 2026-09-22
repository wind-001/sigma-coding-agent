"""终端流式渲染器：把 loop 的观测事件逐条写到 stdout。

P1 **不做 TUI**（计划 0 节）：没有面板、没有鼠标交互、没有滚动区域。
它就是一行行往外写——这已经满足"看到过程"这个需求，
而 TUI 需要把渲染与输入解耦成一整套组件，那是独立批次。

渲染器为什么必须**宽容**（门槛 G34）
    它在 loop 的调用栈里。**渲染器抛异常会让整轮对话前功尽弃**，
    而"少显示一行"的代价小到可以忽略。所以未知事件是打印一行提示，不是崩。

不加 ANSI 颜色的原因
    ``cmd.exe`` 对转义序列的支持不一致（Windows Terminal 支持，conhost 老版本不支持），
    不支持时会把转义序列原样打印成乱码。
    **渲染的健壮性优先于好看**——颜色等终端能力检测稳定后再加。
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from sigma_agent.observe import (
    LoopEvent,
    LoopObserver,
    TextChunk,
    ThinkingChunk,
    ToolEnd,
    ToolStart,
    TurnEnd,
)

ARGS_PREVIEW_CHARS = 120

CONTEXT_WARN_TOKENS = 24_000


def _args_preview(arguments: dict[str, Any]) -> str:
    """把工具参数压成一行。参数值可能很长（write 的 content 就是整个文件）。"""
    try:
        text = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) <= ARGS_PREVIEW_CHARS:
        return text
    return text[:ARGS_PREVIEW_CHARS] + " …"


class TerminalRenderer(LoopObserver):
    """把事件写成人类可读的一行行输出。

    ``stream`` 可注入是为了测试：渲染器的输出必须能被断言，
    否则"流式输出"就只是"看起来在动"而无法验证。
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def on_event(self, event: LoopEvent) -> None:
        if isinstance(event, TextChunk):
            # 文本**不换行**：模型自己会输出换行，渲染器不能替它决定
            self._write(event.text)
        elif isinstance(event, ThinkingChunk):
            self._write(f"· {event.text}")
        elif isinstance(event, ToolStart):
            self._write(f"\n⏺ {event.name}({_args_preview(event.arguments)})\n")
        elif isinstance(event, ToolEnd):
            mark = "✓" if event.ok else "✗"
            self._write(f"  {mark} {event.name}: {event.preview}\n")
        elif isinstance(event, TurnEnd):
            self._write(
                f"\n[{event.status} · {event.rounds} 轮 · "
                f"prompt {event.prompt_tokens} / completion {event.completion_tokens}]\n"
            )
            # 风险 R1：P2-4 起有压缩了，所以撞到这条线意味着
            # **压缩没有按预期触发**（窗口配得太大 / 策略被关掉），
            # 而不是"P1 时代那样必然发生"。文案改成了可执行的排查方向。
            if event.prompt_tokens >= CONTEXT_WARN_TOKENS:
                self._write(
                    f"  ⚠ 上下文已达 {event.prompt_tokens} token。"
                    "若还在持续增长，说明压缩没有触发——"
                    "检查 context window 配置（默认窗口是保守下限）。\n"
                )
        else:
            self._write(f"  (? 未知事件 {type(event).__name__})\n")

    def _write(self, text: str) -> None:
        self._stream.write(text)
        self._stream.flush()
