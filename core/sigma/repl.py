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
"""

from __future__ import annotations

import asyncio

from sigma.sdk import InteractiveSession

PROMPT = "σ> "

EXIT_WORDS = {"exit", "quit", ":q", "退出"}

HELLO = (
    "输入任务后回车执行；exit / quit 或 Ctrl+C 退出。\n"
    "注意：一条任务跑完才能输入下一条——**不支持中途打断**（P3）。"
)


async def run_repl(
    session: InteractiveSession,
    *,
    prompt: str = PROMPT,
) -> int:
    """跑交互循环，返回退出码（恒为 ``0``）。

    渲染**不在这里**——它由 loop 的 observer 负责。REPL 只管输入与生命周期，
    两者分开后，渲染逻辑可以被一次性模式复用（``-p`` 走同一个渲染器）。
    """
    print(HELLO)
    while True:
        try:
            line = await asyncio.to_thread(input, prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        text = line.strip()
        if not text:
            continue
        if text.lower() in EXIT_WORDS:
            return 0

        try:
            await session.send(text)
        except Exception as exc:  # 见模块 docstring：错误要打印，但不要终结会话
            print(f"[本轮失败，会话继续] {type(exc).__name__}: {exc}")
