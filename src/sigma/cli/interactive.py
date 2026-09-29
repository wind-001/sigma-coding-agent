"""交互决策的按键选择器(P3-批次2 追加,星辰拍板)。

**范围边界(与"不做 TUI"决定的关系)**:本模块只服务两个阻塞等人的
交互点——审批确认(允许一次/总是允许/拒绝)与 ask_user 选项列表。
原位小刷新(选项区几行,ANSI 光标上移重画)属于交互面,不属于渲染层;
**不做任何全屏/备用屏**。

按键表(prompt_toolkit 归一化跨平台差异):
    Tab / ↓ / →   下一项(环绕)
    ↑ / ← / Shift+Tab  上一项(环绕)
    Enter          确认
    数字 1–9        直选
    Esc / Ctrl+C   取消(返回 None;审批侧把取消当"拒绝",ask_user 侧回退推荐项)

非 TTY(评测 / 管道 / CI)→ :func:`is_tty_terminal` 为 False,
调用方走 ``choose_option`` 的编号输入降级——既有测试与 CI 行为不变。

**实测口径**(prompt_toolkit 3.0.53,Windows pipe):Tab 解析为
``Keys.ControlI``(同一个字节)、Enter 为 ``Keys.ControlJ``;单独的 ESC
在 VT 解析器里要等后续字节才判定,空闲超时后用 ``flush_keys`` 冲出
``Keys.Escape``——Windows 真控制台里 ESC 是独立虚拟键,无此问题。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from prompt_toolkit.input import create_input
from typing import Any

#: 选择器选项上 movement 键的语义。Tab 以 ControlI 形态出现(同一字节)。
_NEXT_KEYS = {"tab", "controli", "down", "right"}
_PREV_KEYS = {"up", "left", "s-tab"}


def is_tty_terminal() -> bool:
    """stdin/stdout 是否都是真终端。不是 → 一律走编号输入降级。"""
    try:
        return bool(sys.stdin.isatty()) and bool(sys.stdout.isatty())
    except Exception:
        return False


def _move_selection(index: int, count: int, key: str) -> int:
    """纯函数:movement 键 → 新下标(环绕)。其余按键返回原下标。

    单独抽出是为了离线可测:按键到状态的映射不依赖终端。
    """
    if count <= 0:
        return 0
    if key in _NEXT_KEYS:
        return (index + 1) % count
    if key in _PREV_KEYS:
        return (index - 1) % count
    return index


def _draw_options(
    options: list[str], index: int, *, recommended_index: int | None, final: bool
) -> None:
    """画选项区。调用方保证光标停在选项区首行之前。"""
    width = max(20, shutil.get_terminal_size().columns - 4)
    lines: list[str] = []
    for position, option in enumerate(options):
        cursor = "›" if position == index else " "
        mark = "  ← 推荐" if position == recommended_index else ""
        suffix = "  ✔" if final and position == index else ""
        text = f" {cursor} [{position + 1}] {option}{mark}{suffix}"
        if len(text) > width:
            text = text[: width - 1] + " …"
        lines.append(text)
    # 行间必须有换行:\r\x1b[2Kr\r\x1b[2Kx1b[2K 只清当前行、不下移——漏了换行,
    # 每个选项覆盖前一个,只剩最后一条可见(真机截图抓到的);
    # 超宽行截断:折行会让"光标上移 N 行"对不齐,重画叠加错位。
    sys.stdout.write("\r\x1b[2K" + "\r\x1b[2K".join("\r\x1b[2K" + line for line in lines) + "\n")
    sys.stdout.flush()


def _select_sync(
    title_lines: list[str],
    options: list[str],
    *,
    recommended_index: int | None,
    input_factory: Callable[[], Any],
) -> int | None:
    """阻塞版按键循环(prompt_toolkit 低层 API)。由 :func:`select_option` 放线程里跑。"""
    from prompt_toolkit.keys import Keys

    # create_input 已在模块级 import;这里不需要(也不该)再 import 一份。
    inp = input_factory() if input_factory is not None else create_input()
    index = recommended_index if recommended_index is not None else 0
    if not (0 <= index < len(options)):
        index = 0

    for line in title_lines:
        sys.stdout.write(line + "\n")
    _draw_options(options, index, recommended_index=recommended_index, final=False)

    raw_mode = getattr(inp, "raw_mode", None)
    from contextlib import ExitStack

    with ExitStack() as stack:
        # 不调 attach():它要求运行中的事件循环,而本循环跑在线程里;
        # read_keys(阻塞读)不依赖 attach(pipe 实测;控制台输入自带读取器)。
        if raw_mode is not None:
            stack.enter_context(raw_mode())

        pending_esc = False
        idle_since: float | None = None
        while True:
            key_presses = inp.read_keys()
            if not key_presses:
                # 空转:独立 ESC 在 VT 解析器里要等后续字节;空闲超时后
                # 用 flush_keys 把它冲成 Keys.Escape(Windows 原生无此问题)。
                if pending_esc:
                    for kp in inp.flush_keys():
                        if kp.key is Keys.Escape:
                            return None
                    pending_esc = False
                if idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > 0.05:
                    pending_esc = False
                    idle_since = None
                time.sleep(0.02)
                continue
            idle_since = None
            for press in key_presses:
                if press.key is Keys.Escape:
                    return None
                if press.key is Keys.ControlC:
                    return None
                if press.key in (Keys.Enter, Keys.ControlJ):
                    _draw_options(
                        options, index, recommended_index=recommended_index, final=True
                    )
                    return index
                # 字符键(数字/字母)的 key 字段是 str,特殊键才是 Keys 枚举;
                # 枚举 name 是驼峰(Keys.ControlI),键表是小写——统一 lower
                name = (press.key.name if hasattr(press.key, "name") else "").lower()
                data = press.data or ""
                if data == "\x1b":
                    pending_esc = True
                    continue
                if data.isdigit():
                    digit = int(data)
                    if 1 <= digit <= len(options):
                        return digit - 1
                    continue
                new_index = _move_selection(index, len(options), name)
                if new_index != index:
                    index = new_index
                    sys.stdout.write(f"\x1b[{len(options)}A")
                    _draw_options(
                        options, index, recommended_index=recommended_index, final=False
                    )
    return None  # pragma: no cover — while True 只有 return 出口


async def select_option(
    title_lines: list[str],
    options: list[str],
    *,
    recommended_index: int | None = None,
    input_factory: Callable[[], Any] | None = None,
    interactive: bool | None = None,
) -> int | None:
    """异步门面:阻塞循环放到线程里,不卡事件循环。

    ``interactive``:None = 自动探测 TTY;False = 直接返回 None(调用方走
    编号输入降级);True = 强制按键模式(测试用 pipe input 注入)。
    """
    if not options:
        return None
    if interactive is None:
        interactive = is_tty_terminal()
    if not interactive:
        return None
    return await asyncio.to_thread(
        _select_sync,
        title_lines,
        options,
        recommended_index=recommended_index,
        input_factory=input_factory if input_factory is not None else create_input,
    )


async def choose_option(
    title_lines: list[str],
    options: list[str],
    *,
    recommended_index: int | None = None,
    interactive_fallback: Callable[[str], Awaitable[str]] | None = None,
    input_factory: Callable[[], Any] | None = None,
    interactive: bool | None = None,
) -> int | None:
    """统一入口:真终端走按键选择;否则用 ``interactive_fallback`` 编号输入降级。

    降级提示由本函数拼(标题 + 编号选项),fallback 只管取一行文本;
    解析规则:序号 1–N → 下标;选项原文 → 下标;其他/空 → None。
    """
    if interactive is None:
        interactive = is_tty_terminal()
    if interactive:
        return await select_option(
            title_lines,
            options,
            recommended_index=recommended_index,
            input_factory=input_factory,
            interactive=True,
        )
    if interactive_fallback is None:
        return None
    numbered = "\n".join(
        f"  [{position + 1}] {option}" for position, option in enumerate(options)
    )
    prompt = "\n".join(title_lines) + "\n" + numbered + "\n  选择序号(空 = 取消)> "
    raw = (await interactive_fallback(prompt)).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    for position, option in enumerate(options):
        if raw == option:
            return position
    return None
