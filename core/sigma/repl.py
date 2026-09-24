"""简版 REPL：**同步输入、逐条执行、跨轮累积历史**。

它**不是** Pi 那种带 steering 的交互（P3）
    一条任务要跑完整轮（可能多轮工具调用）才能输入下一条，
    **中途不能插话、不能取消**。这个限制写进启动横幅，不假装支持。

输入为什么用 ``asyncio.to_thread``（W10 / 风险 R2）
    ``input()`` 是同步阻塞调用。直接在协程里调它会**卡住整个事件循环**，
    于是工具执行期间的流式输出一顿一顿——症状是"输出卡住了"，
    但根因在输入，不在输出。丢到线程里就两不相扰。

为什么 ``send`` 抛异常时**继续会话**而不是退出
    网络抖动、provider 限流都会抛，而它们**不意味着会话坏了**。
    直接退出会丢掉全部历史——用户刚聊了十轮的上下文没了。
    错误照样打印出来（不掩盖），只是不让它终结会话。

斜杠命令（P5）
    以 ``/`` 开头的输入**不进模型**，而是本地分派（见 ``COMMANDS``）。
    **未知命令报错 + 打帮助，绝不静默发给模型**——这是本批的一次行为变更：
    在 P5 之前 ``/help`` 会被当任务描述发给模型，而模型会**很配合地
    假装自己是命令行**，回一段格式漂亮的"帮助"——用户于是以为命令生效了。
    提示词里确实有"`/` 开头是什么？"的歧义空间，但代价不对称：
    把命令发给模型的坏处是**静默的错误行为**，而报错的坏处只是多打两行字。

    惰性求值：命令存的是**调用签名**（``(fn, 要不要 manager)``）而不是
    已经绑定的函数对象。这样 REPL 循环里 ``await session.send(...)`` 那句
    仍然看得见 manager——写死成闭包会让"当前会话"在第一次拿列表时就被冻结。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sigma_ai import stamps

if TYPE_CHECKING:  # 只为注解；运行时不 import，免得形成环
    from sigma.cli import SessionManager

PROMPT = "σ> "

EXIT_WORDS = {"exit", "quit", ":q", "退出"}

#: 命令前缀。**只有一个字符**，所以判据就是 ``text.startswith("/")``。
COMMAND_PREFIX = "/"

HELLO = (
    "输入任务后回车执行；/help 看命令，exit / quit 或 Ctrl+C 退出。\n"
    "注意：一条任务跑完才能输入下一条——**不支持中途打断**（P3）。"
)


class _ReplSession(Protocol):
    """REPL 需要会话提供的最小接口。

    **为什么不直接注解 ``InteractiveSession``**：REPL 只调 ``send`` 一个方法，
    而 ``InteractiveSession`` 有二十来个构造参数——用协议能让测试传一个
    三行的假会话进来（``tests/test_sigma_repl.py`` 就是这么做的），
    不必为了测一行命令去真造一个带 provider 的会话。

    这也顺带**划清了 REPL 与会话的边界**：REPL 不会去读 ``session.context``
    或 ``session.checkpoint``——那些是 sdk 的内部状态，
    从 REPL 够到它们就等于绕过了"只有一处组装"。
    """

    async def send(self, task: str) -> object:
        ...


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def _now_text(epoch: float) -> str:
    """把 mtime 打成 ``MM-DD HH:MM``。

    **用 ``stamps.split`` 而不是自己 ``datetime.fromtimestamp``**：
    会话文件里的时间戳格式演进过一次（int 秒 → 本地可读串），
    而"换算在哪做"已经被收敛到 ``stamps`` 一处。在这里再写一遍
    ``fromtimestamp``，下次改格式时它会**不跟着改**，
    症状是列表里的时间与文件里的时间对不上。

    **不显示年份**：``/sessions`` 的每一行都要留出空间给摘要，
    而"去年 12 月的会话"在列表里本来就排在最底下。真需要确切时间时，
    id 本身就带完整时间戳（``YYYYMMDD-HHMMSS.mmm-xxxx``）——
    两处给不同的精度，是刻意的分工。
    """
    moment, _ = stamps.split(epoch)
    return f"{moment.month:02d}-{moment.day:02d} {moment.hour:02d}:{moment.minute:02d}"


def _size_text(size: int) -> str:
    """文件大小，紧凑表示。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _switch_target(
    argument: str, index_map: dict[int, str]
) -> str | None:
    """把 ``/switch`` 的参数解析成一个会话 id。

    **两种引用方式**（详规 D-S5）：

    * **序号**：引用**最近一次 ``/sessions`` 输出**里的行号。序号只在
      那之后有效——映射由 ``/sessions`` 重新填。这是刻意的：
      序号必须与**用户看得见的那张表**对应，若每次都重新 `list()` 再编号，
      两次命令之间新产生的会话会把所有序号顶掉，
      于是"第 3 个"变成了另一个会话——**而用户完全看不出来**。
    * **完整 id**：直接给 id。这条路径不依赖任何缓存，所以永远可用。

    **为什么非法输入返回 ``None`` 而不是抛 ``ValueError``**：
    调用方要打印的是"没找到 + 提示敲 /sessions"，与"id 不存在"是同一种回执。
    为两种"找不到"写两套错误处理只会让其中一套失修。
    """
    if not argument:
        return None
    if argument.isdigit():
        return index_map.get(int(argument))
    return argument


def print_help() -> None:
    """命令清单。未知命令与 ``/help`` **共用这一份**——
    两份清单的结局是其中一份慢慢过期，而用户按过期的那份敲命令会得到"未知命令"。
    """
    print("命令：")
    print("  /sessions           列出最近的会话（当前会话标 *）")
    print("  /switch <序号|id>   切换到某个会话，历史与快照都接上")
    print("  /new                开一个新会话")
    print("  /help               这份清单")
    print("  exit / quit         退出（Ctrl+C 也一样）")


async def _cmd_sessions(manager: SessionManager, index_map: dict[int, str]) -> None:
    """``/sessions``：列最近 20 个会话，当前会话打 ``*``。

    **同时刷新 ``index_map``**——这是序号引用的唯一来源。
    列表为空时**也要清空映射**：留着上一次的序号，会让
    ``/switch 1`` 切到一个已经不在列表里的会话，而用户以为它还在。
    """
    previews = manager.list()
    index_map.clear()
    if not previews:
        print(f"还没有任何会话（{manager.sessions_root} 是空的）。")
        return

    current = manager.current_id
    print(f"最近 {len(previews)} 个会话（新 → 旧）：")
    for index, preview in enumerate(previews):
        index_map[index] = preview.id
        mark = "*" if preview.id == current else " "
        summary = preview.first_user_text or "（还没有用户消息）"
        print(
            f" {mark}[{index}] {preview.id}"
            f"  {_now_text(preview.modified)}"
            f"  {_size_text(preview.size):>9}"
            f"  {preview.message_count:>3} 条"
            f'  "{summary}"'
        )
    print("用 /switch <序号|id> 切过去。")


async def _cmd_switch(manager: SessionManager, index_map: dict[int, str], argument: str) -> None:
    """``/switch <序号|id>``：切换会话，**历史与 checkpoint 都接上**。

    找不到时**报错但不崩**：这是人的输入，敲错一位太正常了。
    崩掉的 REPL 会连带丢掉当前会话的内存状态——代价远大于一次提示。
    """
    target = _switch_target(argument, index_map)
    if target is None:
        # 分两种说清楚，因为它们要用户做的事不一样：
        # 序号没引用上 → 敲 /sessions；id 敲错了 → 敲 /sessions 对一下拼写。
        if argument.isdigit():
            print(f"没有序号 {argument}——序号只在最近一次 /sessions 输出后有效，先敲 /sessions。")
        elif not argument:
            print("用法：/switch <序号|id>。先敲 /sessions 看有哪些会话。")
        else:
            print(f"没有会话 {argument!r}：{Path(manager.sessions_root)} 下没有这个文件。")
            print("先敲 /sessions 对一下 id。")
        return

    outcome = manager.switch_to(target)
    if not outcome.ok:
        print(f"没有会话 {outcome.session_id!r}：文件不存在（可能在另一个终端里被删了）。")
        print("先敲 /sessions 看现在还有哪些。")
        return

    if outcome.switched:
        print(f"已切换到会话 {outcome.session_id}（续上 {outcome.messages} 条消息）。")
        # **切换后必须把序号映射清掉**：序号对应的是"上一次 /sessions 那张表"，
        # 而切换会让 `*` 换行、新会话也可能刚产生。留着旧映射，
        # 下一次 `/switch 1` 会切到一个与用户以为的不一致的会话。
        index_map.clear()
    else:
        print(
            f"已经是会话 {outcome.session_id} 了（重新载入 {outcome.messages} 条历史）。"
        )


async def _cmd_new(manager: SessionManager, index_map: dict[int, str]) -> None:
    """``/new``：开新会话，**立即生效**。

    新 id 是生成的（不是"等你下一条输入时再建"）：用户敲完就应当在
    ``/sessions`` 里看到它——"我开了个新会话但它不在列表里"
    会让人怀疑命令没生效。
    """
    session_id = manager.new()
    index_map.clear()
    print(f"已开新会话 {session_id}。")


async def _cmd_help(manager: SessionManager, index_map: dict[int, str]) -> None:
    print_help()


#: 命令表：别名 → (执行体, 参数名)。
#:
#: **别名写进同一张表**（而不是在分派处写一堆 ``if name in {"list", "ls"}``）：
#: 别名与主名的行为必须完全一致，而分派处的 ``if`` 是最容易漏掉一处的地方
#: （``/help`` 里列了别名、分派忘了接，用户就会得到"未知命令"）。
#:
#: 值的类型是 ``Callable[..., Awaitable[None]]`` 而不是 ``Callable[..., object]``：
#: 分派那里要 ``await``，而 ``object`` 会让 mypy 报"await 一个 object"
#: （实测：第一版就是这么写的，3 条 mypy 错里有 2 条来自这里）。
#: 命令全部是协程，因为它们都可能要 await I/O——现在只有 ``/switch`` 会读盘，
#: 但把签名统一成 async 让"加一个要读盘的命令"不必改分派层。
CommandHandler = Callable[..., Awaitable[None]]

COMMANDS: dict[str, tuple[CommandHandler, str]] = {
    "sessions": (_cmd_sessions, ""),
    "list": (_cmd_sessions, ""),
    "switch": (_cmd_switch, "序号|id"),
    "resume": (_cmd_switch, "序号|id"),
    "new": (_cmd_new, ""),
    "help": (_cmd_help, ""),
    "h": (_cmd_help, ""),
    "?": (_cmd_help, ""),
}


async def _dispatch(manager: SessionManager, index_map: dict[int, str], text: str) -> None:
    """把一行 ``/xxx arg`` 分派到命令。

    **命令名大小写不敏感**（``/Sessions`` 也认）：用户顺手用了大写不该被拒，
    而会话 id 是**大小写敏感**的（时间戳里有小写字母），所以只对命令名
    ``lower()``，参数原样传下去——这个区分很重要，
    对参数也 lower 会悄悄把 id 改成另一个（不存在的）会话。
    """
    body = text[len(COMMAND_PREFIX):]
    name, _, argument = body.partition(" ")
    entry = COMMANDS.get(name.strip().lower())
    if entry is None:
        # **报错 + 打帮助，绝不静默发给模型**（模块 docstring 里那条行为变更）。
        # 顺带打一份帮助：用户敲错命令时最想要的就是"有哪几个"。
        print(f"未知命令：{text}")
        print()
        print_help()
        return
    handler, _signature = entry
    argument = argument.strip()
    if argument:
        await handler(manager, index_map, argument)
    else:
        await handler(manager, index_map)


async def run_repl(
    manager: SessionManager,
    *,
    prompt: str = PROMPT,
) -> int:
    """跑交互循环，返回退出码（恒为 ``0``）。

    渲染**不在这里**——它由 loop 的 observer 负责。REPL 只管输入与生命周期，
    两者分开后，渲染逻辑可以被一次性模式复用（``-p`` 走同一个渲染器）。

    参数是 **manager 而不是 session**（P5）：``/switch`` 要换掉整个会话对象，
    拿到 ``session`` 就没法换了。REPL 每轮从 manager 取当前会话，
    从不缓存它。
    """
    print(HELLO)
    #: 序号 → 会话 id。**只在两次 ``/sessions`` 之间有意义**，见 ``_switch_target``。
    index_map: dict[int, str] = {}
    while True:
        try:
            line = await asyncio.to_thread(input, prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        text = line.strip()
        if not text:
            continue
        if text.startswith(COMMAND_PREFIX):
            await _dispatch(manager, index_map, text)
            continue
        if text.lower() in EXIT_WORDS:
            return 0

        try:
            await manager.current.send(text)
        except Exception as exc:  # 见模块 docstring：错误要打印，但不要终结会话
            print(f"[本轮失败，会话继续] {type(exc).__name__}: {exc}")
