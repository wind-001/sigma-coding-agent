"""终端渲染钩子：订阅日志事件，把 loop 的过程逐条写向 stdout。

**P4-批次6 起，渲染是一个钩子**（星辰拍板：日志记录与告知是钩子的义务）。
它订阅五类日志事件（TextChunk / ThinkingChunk / ToolStart / ToolEnd /
TurnEnd），与持久化钩子平级地注册进 HookManager——不注册就没有输出，
这对评测与子 agent 测试恰恰是想要的静默。

**P1 不做 TUI 的决定不翻案**：没有 rich.Live / Progress / Spinner 等重绘
组件，一行行往外写。rich 在这里只承担**终端能力检测**——有 TTY 就上色
（conhost 老终端 / 管道 / CI 自动降级纯文本），这正是原 docstring 里
"颜色等终端能力检测稳定后再加"的兑现。

三条渲染纪律

1. **模型文本绕过 rich**（Q3/G98）：TextChunk 逐字直写流。rich 的
   markup 会把模型输出里的 ``[方括号]`` 当样式吃掉、highlight 会把
   数字/路径擅自变色——模型说什么就写什么，渲染器不替它排版。
2. **结构行同形**：符号（⏺ ✓ ✗）与行形状与 P1 逐字节一致，只加颜色；
   ``markup=False, highlight=False`` 下,模型数据里的方括号原样保留。
   ``soft_wrap=True`` 保持单行(管道/重定向里也不折行)。
3. **渲染器必须宽容**（G34/G99）：它在 loop 的调用栈里，渲染抛异常
   会让整轮对话前功尽弃。rich 层任何异常都降级为旧式裸文本输出，
   未知事件打印一行提示，都不是崩。
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from rich.console import Console

from sigma_agent.hooks import (
    BaseHook,
    HookEvent,
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


def _plain_line(event: HookEvent) -> str:
    """事件的**旧式裸文本形态**——G99 的降级出口，也是行为基线。"""
    if isinstance(event, TextChunk):
        return event.text
    if isinstance(event, ThinkingChunk):
        return f"· {event.text}"
    if isinstance(event, ToolStart):
        return f"\n⏺ {event.name}({_args_preview(event.arguments)})\n"
    if isinstance(event, ToolEnd):
        mark = "✓" if event.ok else "✗"
        return f"  {mark} {event.name}: {event.preview}\n"
    if isinstance(event, TurnEnd):
        line = (
            f"\n[{event.status} · {event.rounds} 轮 · "
            f"prompt {event.prompt_tokens} / completion {event.completion_tokens}]\n"
        )
        if event.prompt_tokens >= CONTEXT_WARN_TOKENS:
            line += (
                "  ⚠ 上下文已达 "
                f"{event.prompt_tokens} token。"
                "若还在持续增长，说明压缩没有触发——"
                "检查 context window 配置（默认窗口是保守下限）。\n"
            )
        return line
    return f"  (? 未知事件 {type(event).__name__})\n"


class TerminalRenderer(BaseHook):
    """渲染钩子：把日志事件写成人类可读的一行行输出。

    ``stream`` 可注入是为了测试：渲染器的输出必须能被断言，
    否则"流式输出"就只是"看起来在动"而无法验证。
    非 TTY（StringIO / 管道 / CI）下 rich 自动无色——断言的是稳定纯文本。
    """

    name = "terminal-render"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        # markup=False / highlight=False 是 Console 级默认：任何承载模型
        # 数据的行都不会被样式解释（陷阱①，G98 钉住）。颜色只经 style= 参数。
        self._console = Console(
            file=self._stream,
            markup=False,
            highlight=False,
            emoji=False,
        )

    def events(self) -> tuple[type[HookEvent], ...]:
        return (TextChunk, ThinkingChunk, ToolStart, ToolEnd, TurnEnd)

    def on_event(self, event: HookEvent) -> None:
        """渲染一个事件。**绝不抛**——降级出口见 :meth:`_render` 的调用方。"""
        try:
            self._render(event)
        except Exception:
            # G99：rich 层任何异常（宽度探测 / 编码 / 内部缺陷）都降级成
            # 旧式裸文本。"少一次颜色"的代价远小于"整轮对话不可见"。
            self._raw(_plain_line(event))

    def _render(self, event: HookEvent) -> None:
        if isinstance(event, TextChunk):
            # 模型文本**绕过 rich**：逐字直写，不换行——模型自己会输出换行，
            # 渲染器不能替它决定，更不能替它排版（Q3/G98）。
            self._raw(event.text)
        elif isinstance(event, ThinkingChunk):
            self._styled(f"· {event.text}", style="dim", end="")
        elif isinstance(event, ToolStart):
            self._styled(
                f"\n⏺ {event.name}({_args_preview(event.arguments)})",
                style="cyan",
            )
        elif isinstance(event, ToolEnd):
            mark = "✓" if event.ok else "✗"
            self._styled(
                f"  {mark} {event.name}: {event.preview}",
                style="green" if event.ok else "red",
            )
        elif isinstance(event, TurnEnd):
            self._styled(
                f"\n[{event.status} · {event.rounds} 轮 · "
                f"prompt {event.prompt_tokens} / completion {event.completion_tokens}]"
            )
            # 风险 R1：P2-4 起有压缩了，撞到这条线意味着**压缩没有按预期触发**
            #（窗口配得太大 / 策略被关掉），不是 P1 时代那样必然发生。
            if event.prompt_tokens >= CONTEXT_WARN_TOKENS:
                self._styled(
                    f"  ⚠ 上下文已达 {event.prompt_tokens} token。"
                    "若还在持续增长，说明压缩没有触发——"
                    "检查 context window 配置（默认窗口是保守下限）。",
                    style="yellow",
                )
        else:
            # G34：未知事件是提示，不是崩——渲染器必须宽容。
            self._styled(f"  (? 未知事件 {type(event).__name__})")

    def _styled(self, text: str, *, style: str | None = None, end: str = "\n") -> None:
        """结构行：rich 上色（非 TTY 自动无色），形状与旧式输出逐字节一致。"""
        self._console.print(text, style=style, end=end, soft_wrap=True)
        self._stream.flush()

    def _raw(self, text: str) -> None:
        """模型文本与降级出口：逐字直写流，不经 rich。"""
        self._stream.write(text)
        self._stream.flush()
