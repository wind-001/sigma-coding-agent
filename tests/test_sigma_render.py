"""终端渲染器的测试（门槛 G34 / G36 的渲染侧）。

``stream`` 注入 ``StringIO`` 是这些用例能存在的前提——
渲染器的输出必须能被断言，否则"流式输出"只是"看起来在动"。
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO

from sigma.render import TerminalRenderer
from sigma_agent.hooks import TextChunk, ToolEnd, ToolStart, TurnEnd
from sigma_ai.messages import TextBlock


def _tool_end(name: str, ok: bool, preview: str) -> ToolEnd:
    """渲染门槛的 ToolEnd 构造器:补上 loop 才关心的 message 载荷。"""
    from sigma_agent.agent_messages import ToolResultAgentMessage

    return ToolEnd(
        name=name,
        ok=ok,
        preview=preview,
        message=ToolResultAgentMessage(
            tool_call_id="c-test",
            tool_name=name,
            content=[TextBlock(text=preview)],
            is_error=not ok,
            timestamp="2026-09-26T00:00:00.000",
        ),
    )


def _render(*events: object) -> str:
    buf = StringIO()
    renderer = TerminalRenderer(stream=buf)
    for event in events:
        renderer.on_event(event)  # type: ignore[arg-type]
    return buf.getvalue()


def test_text_is_written_verbatim_without_extra_newline() -> None:
    """文本**逐字**写出、不加换行——模型自己会输出换行。"""
    out = _render(TextChunk(text="我正在改"), TextChunk(text="第 3 行\n"))
    assert out == "我正在改第 3 行\n"


def test_failed_tool_is_marked_with_cross_and_text_is_shown() -> None:
    """门槛 G36（渲染侧）：失败标 ✗，**且失败原因要写出来**。

    只画一个 ✗ 等于什么都没说——人（和看日志的人）需要知道败在哪。
    """
    out = _render(_tool_end("bash", False, "命令失败，退出码 1。"))
    assert "✗" in out
    assert "bash" in out
    assert "退出码 1" in out

    ok_out = _render(_tool_end("bash", True, "total 3"))
    assert "✓" in ok_out


def test_tool_start_shows_name_and_arguments() -> None:
    out = _render(ToolStart(name="grep", arguments={"pattern": "OLD"}, call_id="c1"))
    assert "grep" in out
    assert "OLD" in out


def test_unknown_event_does_not_crash_the_renderer() -> None:
    """门槛 G34：未知事件**打印一行提示**，绝不抛。

    渲染器在 loop 的调用栈里——它抛异常，整轮对话的输出就没了。
    "少显示一行"的代价远小于此。
    """

    @dataclass(frozen=True)
    class WeirdEvent:
        pass

    out = _render(WeirdEvent())
    assert "未知事件" in out


def test_large_context_gets_a_warning() -> None:
    """风险 R1：上下文逼近上限时提醒（提醒不是错误，不打断）。"""
    out = _render(TurnEnd(status="completed", rounds=1, prompt_tokens=30_000, completion_tokens=10))
    assert "⚠" in out
    assert "30,000" in out or "30000" in out

    quiet = _render(TurnEnd(status="completed", rounds=1, prompt_tokens=500, completion_tokens=10))
    assert "⚠" not in quiet


# ---------------------------------------------------------------------------
# G98 / G99(P4-批次6):rich 化之后的新门槛
# ---------------------------------------------------------------------------


def test_model_text_with_brackets_is_verbatim() -> None:
    """门槛 G98:模型数据里的方括号**逐字保留**。

    rich 的 markup 会把 ``[bold]`` 当样式吃掉、highlight 会把数字/路径
    擅自变色。模型写什么终端就显示什么——渲染器不替它排版。
    注入:任何一处允许 markup/highlight(去掉 markup=False)→ 本条红。
    """
    out = _render(
        TextChunk(text="查看 [1] 与 [bold]不是样式[/bold]\n"),
        ToolStart(name="write", arguments={"path": "[a].txt", "content": "x"}, call_id="c1"),
        _tool_end("grep", True, "命中 [pattern] 2 处"),
    )
    assert "[1]" in out
    assert "[bold]不是样式[/bold]" in out
    assert "[a].txt" in out
    assert "[pattern]" in out


def test_render_failure_degrades_to_plain_output() -> None:
    """门槛 G99:渲染层(rich)抛任何异常 → 降级为旧式裸文本,**不冒泡**。

    渲染钩子在 loop 调用栈里,渲染崩 = 整轮对话不可见。降级出口保证
    "rich 坏了"最多丢颜色,不丢过程。
    注入:让 Console.print 抛异常 → 若异常冒泡本条红。
    """
    buf = StringIO()
    renderer = TerminalRenderer(stream=buf)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("rich 内部坏了")

    renderer._console.print = _boom  # type: ignore[method-assign]

    renderer.on_event(ToolStart(name="bash", arguments={"command": "ls"}, call_id="c1"))
    renderer.on_event(_tool_end("bash", False, "退出码 1"))

    out = buf.getvalue()
    assert "⏺ bash" in out
    assert "✗ bash" in out
    assert "退出码 1" in out
