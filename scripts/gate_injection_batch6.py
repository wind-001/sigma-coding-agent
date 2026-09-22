"""批次 6 门槛注入实验：逐条证伪 G32–G36。

沿用批次 2–4 的框架（在真实仓库上改、跑、`finally` 还原），
**不另写一份** ``Repo``——验证工具自身会腐化，两份实现意味着
修一个忘另一个。

| 编号 | 门槛 | 注入 |
| --- | --- | --- |
| E30 | G32 | 观测被静默丢弃 → 事件序列为空 |
| E31 | G33 | `_notify` 不判 None → 无观察者时崩 |
| E32 | G34 | 渲染器遇未知事件抛异常 → 整轮输出没了 |
| E33 | G35 | `send` 不追加产出 → 每轮都是全新会话 |
| E34 | — | CLI 不判 isatty → 非 TTY 也进 REPL（管道挂住） |

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch6.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

LOOP = "core/sigma_agent/loop.py"
RENDER = "core/sigma/render.py"
SDK = "core/sigma/sdk.py"
CLI = "core/sigma/cli.py"

OBSERVE_TESTS = "tests/test_agent_observe.py"
RENDER_TESTS = "tests/test_sigma_render.py"
REPL_TESTS = "tests/test_sigma_repl.py"
CLI_TESTS = "tests/test_sigma_cli.py"

NOTIFY_BODY = (
    "        if self._observer is not None:\n"
    "            self._observer.on_event(event)"
)


def _inject_e30(repo: Repo) -> None:
    """E30 / G32：``_notify`` 什么都不发。

    观测是**旁听**，坏掉不影响任务成败——所以它坏掉时**没有任何测试会红**，
    除非有专门盯它的用例。这正是门槛要防的形态。
    """
    repo.patch(LOOP, NOTIFY_BODY, "        return  # 注入：观测被静默丢弃")


def _inject_e31(repo: Repo) -> None:
    """E31 / G33：去掉 ``is not None`` 判断。

    没有 observer 时直接在 ``None`` 上调方法 → ``AttributeError``。
    这意味着"观测"从可选功能变成了**必填**，一次性调用全部崩掉。
    """
    repo.patch(
        LOOP,
        NOTIFY_BODY,
        "        self._observer.on_event(event)  # 注入：不判 None",
    )


def _inject_e32(repo: Repo) -> None:
    """E32 / G34：渲染器遇到未知事件直接抛。

    渲染器在 loop 的调用栈里——它抛异常，整轮对话的输出就没了。
    """
    repo.patch(
        RENDER,
        '            self._write(f"  (? 未知事件 {type(event).__name__})\\n")',
        '            raise TypeError(f"未知事件 {type(event).__name__}")  # 注入',
    )


def _inject_e33(repo: Repo) -> None:
    """E33 / G35：``send`` 不把产出追加回历史。

    此后每一轮都是"全新会话"，模型反问"你说的那个文件是哪个？"——
    而用户看不出发生了什么。
    """
    repo.patch(SDK, "        self._context.append(*result.messages)", "        pass  # 注入：不累积历史")


def _inject_e34(repo: Repo) -> None:
    """E34：CLI 不判 stdin 是不是控制台。

    非交互场景（脚本 / CI / DEVNULL）会一路走到 REPL 等输入，
    症状是"卡住"而不是报错。

    **注入方向容易搞反**：这个分支为真时是"退到帮助页"。
    要模拟"忘了判断"，必须让它**永远不进入**——写成 ``if True:`` 的话，
    条件反而变成了恒成立，测试照过，**得到一条假绿**。
    （批次 2–4 的 E25 踩过同一类坑：看起来有道理的注入不等于有效的注入。）

    Windows 上这条尤其要命：``isatty`` 对 NUL 设备返回 True，
    所以"记得判断"还不够，**判断错一样中招**。
    """
    repo.patch(
        CLI,
        "    if (\n"
        "        not args.prompt\n"
        "        and not args.interactive\n"
        "        and not wants_rollback\n"
        "        and not stdin_is_interactive()\n"
        "    ):",
        "    if False:  # 注入：不判 stdin，非交互场景也往下走去 REPL",
    )


def _inject_e35(repo: Repo) -> None:
    """E35 / G36：渲染忽略 ``is_error``，失败也画 ✓。

    终端上一个 ✓ 会让人（和读日志的人）以为工具跑通了——
    而失败原因恰恰是最该被看见的东西（详规 3.6）。
    """
    repo.patch(
        RENDER,
        '            mark = "✓" if event.ok else "✗"',
        '            mark = "✓"  # 注入：忽略 is_error',
    )


EXPERIMENTS = [
    ("E30", "G32  _notify 静默丢弃 → 事件序列为空", f"{OBSERVE_TESTS}::test_loop_emits_full_event_sequence_in_order", _inject_e30),
    ("E31", "G33  _notify 不判 None → 无观察者时崩", f"{OBSERVE_TESTS}::test_observer_is_optional_and_default_impl_is_silent", _inject_e31),
    ("E32", "G34  渲染器遇未知事件抛异常 → 整轮输出没了", f"{RENDER_TESTS}::test_unknown_event_does_not_crash_the_renderer", _inject_e32),
    ("E33", "G35  send 不追加产出 → 每轮都是全新会话", f"{REPL_TESTS}::test_history_survives_across_turns", _inject_e33),
    ("E34", "启动分支  CLI 不判 stdin → 非交互场景进 REPL 卡住", f"{CLI_TESTS}::test_without_prompt_on_non_interactive_stdin_prints_help", _inject_e34),
    ("E35", "G36  渲染忽略 is_error → 失败也画 ✓", f"{RENDER_TESTS}::test_failed_tool_is_marked_with_cross_and_text_is_shown", _inject_e35),
]


def main() -> int:
    for gate, what, target, inject in EXPERIMENTS:
        print(f"[{gate}] 注入：{what}")
        experiment(gate, what, target, inject)

    print()
    print("=" * 78)
    print("批次 6 门槛注入实验结果")
    print("=" * 78)
    ok = 0
    for gate, what, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        if passed:
            ok += 1
        print(f"[{mark}] {gate}  {what}")
        print(f"       → {detail}")
    print("=" * 78)
    print(f"{ok}/{len(RESULTS)} 条门槛被成功证伪（注入后确实变红）")
    RESULTS.clear()
    return 0 if ok == len(EXPERIMENTS) else 1


if __name__ == "__main__":
    sys.exit(main())
