"""交互会话的测试（门槛 G35）。

G35 要证明的是**跨轮累积**：第二轮提问时，第一轮的工具结果还在上下文里。
这条如果失守，交互模式就退化成"每轮都是全新会话"——
症状是模型反问"你说的那个文件是哪个？"，而用户完全不知道发生了什么。

这里用**真实的内置 read 工具**（不是自造的假工具）：
跨轮丢历史的 bug 往往出在"工具结果消息怎么回流"上，用真工具才测得到。

P5 起另有三组测试
    ``/sessions`` / ``/switch`` / ``/new`` / 未知命令。它们与 G35 同文件，
    因为测的是**同一个循环**：REPL 分派。**没有单独建一个
    ``test_sigma_repl_commands.py``**——那样两个文件都要维护一套
    "怎么把一个假会话喂进 REPL"的脚手架，而它们迟早会漂移。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sigma.cli import SessionBinding, SessionManager, build_parser
from sigma.repl import run_repl
from sigma.sdk import InteractiveSession
from sigma_agent.agent_messages import ToolResultAgentMessage
from sigma_agent.registry import ToolRegistry
from sigma_ai.fake import FakeProvider
from sigma_session.sessions import SESSION_SUFFIX
from sigma_session.store import JsonlStore
from sigma_session.tree import SessionTree


def _read_call(path: str = "a.txt") -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": "call_1",
            "name": "read",
            "arguments_delta": f'{{"path": "{path}"}}',
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _text(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_history_survives_across_turns(tmp_path: Path) -> None:
    """门槛 G35：第一轮的工具结果在第二轮之后**仍在历史里**。

    **2026-09-21 改判据：``is`` → ``==``。**

    P2-3 把历史改由 ``SessionTree`` 承载，而树对每条消息做
    "``message_to_dict`` → ``message_from_dict``"的往返
    （即使纯内存模式也这么做——这样**内存路径与落盘路径走同一份代码**，
    某个消息类型读不回来会立刻暴露，而不是等到开了落盘才发现）。

    往返的代价是**对象同一性丢了**，值仍然逐字段相等
    （``model_dump()`` 全等，已实测）。

    这条断言原本的真意是"**没有丢**，不是内容恰好一样"——这个真意没变，
    所以判据换成 ``==`` 依然能钉住它；用 ``is`` 则会因为
    "重建了但内容全对"而误报。

    注意**不能**把它弱化成"历史条数对"：那会放过"第一条被换成另一条"的情况。
    """
    root = tmp_path
    (root / "a.txt").write_text("hello\n", encoding="utf-8")

    provider = FakeProvider.from_rounds(
        [_read_call(), _text("读完了"), _text("是 hello")]
    )
    session = InteractiveSession(
        provider=provider, workspace_root=root, model="fake"
    )

    first = await session.send("读一下 a.txt")
    tool_msg = next(
        m for m in first.messages if isinstance(m, ToolResultAgentMessage)
    )
    # 工具真的读到了内容——否则下面"还在历史里"证明的是一条空结果的留存
    assert any("hello" in getattr(b, "text", "") for b in tool_msg.content)

    await session.send("刚才读到的是什么？")

    assert any(
        m == tool_msg for m in session.history()
    ), "第一轮的工具结果不在历史里——会话每轮都在重建，交互模式失去意义"


# ---------------------------------------------------------------------------
# P5：REPL 斜杠命令
# ---------------------------------------------------------------------------
#
# 这一组测的是**分派**（哪一行进了模型、哪一行被本地处理），不是跑任务——
# 所以 provider 是 ``FakeProvider``，且 transcript 给得刚刚好：
# **多了会暴露"命令被发给了模型"**（那会多消费一个轮次），
# 少了会抛 ``TranscriptExhausted``。两个方向都能抓到错。

#: 固定的会话 id（不用 ``new_session_id()``）：断言里要按 id 找文件，
#: 随机 id 会让断言写成"某个文件里有没有"，那会放过"落到了错误的文件"。
ALPHA = "20260923-100000.000-aaaa"
BETA = "20260923-110000.000-bbbb"


def _manager(
    tmp_path: Path,
    *,
    session_id: str = ALPHA,
    rounds: list[list[dict[str, Any]]] | None = None,
    argv: list[str] | None = None,
) -> SessionManager:
    """造一个可离线跑的 manager。

    ``make_provider`` 注入 ``FakeProvider``：``SessionManager`` 默认会真造
    ``OpenAICompatProvider``（要 base_url 与 key），测试里不该碰网络。
    ``argv`` 传额外旗标（如 ``["--sub-agent"]``）——走真解析器，
    免得手搓 Namespace 与真实字段对不上。
    """
    args = build_parser().parse_args(argv or [])
    return SessionManager(
        args=args,
        workspace=tmp_path,
        base_url="http://fake",
        model="fake",
        api_key="sk-test",
        registry=ToolRegistry(),
        system_prompt="",
        sessions_root=tmp_path / "sessions",
        binding=SessionBinding(session_id, SessionTree(), resumed=False, previous_messages=0),
        shadow_git_dir=None,
        skills_root=None,
        make_provider=lambda: FakeProvider.from_rounds(
            rounds if rounds is not None else [_text("好")]
        ),
    )


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """把 stdin 换成脚本化的输入序列。

    ``run_repl`` 用 ``asyncio.to_thread(input, prompt)`` 读输入，所以这里
    换的是内置 ``input``（用 monkeypatch 而不是真管道：``to_thread`` 里
    读管道在 Windows 上会因缓冲而卡住）。用完后抛 ``EOFError`` 让 REPL
    正常退出——**与用户按 Ctrl+D 是同一条路**。
    """
    remaining = list(lines)

    def fake_input(prompt: str = "") -> str:
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


async def _write_session(
    root: Path, session_id: str, rounds: list[list[dict[str, Any]]]
) -> None:
    """在磁盘上落一个**真的有历史**的会话（用真 provider 跑一轮）。

    ``await`` 而不是 ``asyncio.run``：这些测试本身就在事件循环里跑，
    而 ``asyncio.run`` 会抛 "cannot be called from a running event loop"
    （第一版就是这么错的）。
    """
    session = InteractiveSession(
        provider=FakeProvider.from_rounds(rounds),
        workspace_root=root,
        model="fake",
        session_id=session_id,
        tree=SessionTree.from_store(JsonlStore(root, session_id)),
        project_instructions="",
    )
    await session.send(f"{session_id} 里的任务")


@pytest.mark.asyncio
async def test_sessions_lists_disk_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``/sessions`` 列出磁盘上的会话，**当前那个打 ``*``**。

    打了 ``*`` 才能回答"我现在在哪个会话里"——而这是切换前必须知道的
    （不然用户不知道切走要付出什么）。
    """
    root = tmp_path / "sessions"
    await _write_session(root, ALPHA, [_text("甲的回答")])
    await _write_session(root, BETA, [_text("乙的回答")])

    manager = _manager(tmp_path, session_id=ALPHA)
    _feed(monkeypatch, ["/sessions", "exit"])

    assert await run_repl(manager) == 0
    out = capsys.readouterr().out

    assert ALPHA in out and BETA in out, "两个会话都该出现"
    # 当前会话标 *，另一个不标——逐行断言，免得"某行有星号"就放过
    alpha_line = next(line for line in out.splitlines() if ALPHA in line)
    beta_line = next(line for line in out.splitlines() if BETA in line)
    assert alpha_line.lstrip().startswith("*"), "当前会话没打 *"
    assert not beta_line.lstrip().startswith("*"), "非当前会话打了 *"
    # 首条用户消息摘要是可辨认性的关键（id 是自动生成的，认不出来）
    assert f"{ALPHA} 里的任务" in alpha_line


@pytest.mark.asyncio
async def test_sessions_on_empty_dir_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """空目录下 ``/sessions`` **明说没有**，不是打一个空表头。

    空表头看起来像"命令没生效"，而用户会去怀疑命令拼错了。
    """
    manager = _manager(tmp_path)
    _feed(monkeypatch, ["/sessions", "exit"])

    assert await run_repl(manager) == 0

    assert "还没有任何会话" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_switch_by_index_makes_history_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``/switch <序号>`` 之后，**那个会话的历史真的接上了**。

    这是 P5 最核心的一条：切换的全部意义就是"接上别人的上下文"。
    只换 id 不换树的症状是**模型突然忘了刚才说的一切**，
    而它照样能答——看起来只是"这次答得不好"。

    断言直接看 ``context.build_messages()``：那才是模型实际看到的东西
    （``/sessions`` 的摘要只证明文件里有，不证明模型看到了）。
    """
    root = tmp_path / "sessions"
    await _write_session(root, BETA, [_text("乙的回答")])
    manager = _manager(tmp_path, session_id=ALPHA)

    _feed(monkeypatch, ["/sessions", "/switch 0", "exit"])
    assert await run_repl(manager) == 0

    assert manager.current_id == BETA, "没切过去"
    dumped = "".join(
        m.model_dump_json() for m in manager.current.context.build_messages()
    )
    assert f"{BETA} 里的任务" in dumped, "切过去了但历史没接上（只换了 id）"
    assert "乙的回答" in dumped


@pytest.mark.asyncio
async def test_switch_by_full_id_works_without_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/switch <完整 id>`` **不依赖** ``/sessions`` —— 直接给 id 就能切。

    这条路径必须能用：序号只在两次 ``/sessions`` 之间有效，
    而用户常常已经知道 id（从横幅、从上一个终端里抄的）。
    """
    root = tmp_path / "sessions"
    await _write_session(root, BETA, [_text("乙的回答")])
    manager = _manager(tmp_path, session_id=ALPHA)

    _feed(monkeypatch, [f"/switch {BETA}", "exit"])
    assert await run_repl(manager) == 0

    assert manager.current_id == BETA


@pytest.mark.asyncio
async def test_switch_to_missing_session_reports_and_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """切到不存在的会话 → **报错但不崩，且仍停在原会话**。

    "不崩"是硬要求：REPL 一崩就丢了当前会话**内存里**的全部状态，
    而敲错一个 id 是人的日常。

    "仍停在原会话"更关键：静默切到一个空的新会话，
    用户会以为自己在读旧上下文——那正是最难发现的一类失败。
    """
    manager = _manager(tmp_path, session_id=ALPHA)
    _feed(monkeypatch, ["/switch 不存在的会话", "exit"])

    assert await run_repl(manager) == 0

    out = capsys.readouterr().out
    assert "不存在的会话" in out
    assert manager.current_id == ALPHA, "切换失败却把当前会话换掉了"


@pytest.mark.asyncio
async def test_switch_index_before_listing_explains_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """没敲过 ``/sessions`` 就用序号 → **说清"序号从哪来"**，不是干巴巴的"没有"。

    序号只在最近一次 ``/sessions`` 之后有效，这是刻意的设计
    （序号必须对应**用户看得见的那张表**）。既然设计如此，
    报错就得把这条规则讲出来，否则用户只会觉得"序号怎么时灵时不灵"。
    """
    root = tmp_path / "sessions"
    await _write_session(root, BETA, [_text("乙的回答")])
    manager = _manager(tmp_path, session_id=ALPHA)

    _feed(monkeypatch, ["/switch 0", "exit"])
    assert await run_repl(manager) == 0

    out = capsys.readouterr().out
    assert "没有序号 0" in out
    assert "/sessions" in out, "报错里没告诉用户该敲什么"


@pytest.mark.asyncio
async def test_later_send_lands_in_the_switched_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**门槛 G87**：切换之后发的话落在**新会话**的文件里，旧文件不再增长。

    注入靶：把 ``switch_to`` 改成"只换 id 不换树"（或只改 ``_binding``
    不改 ``_session``），这条必须变红。

    为什么这条比"历史看得见"更硬：``build_messages`` 里有没有旧内容，
    只说明**读**对了；而"新说的话写到哪个文件"说明**写**对了。
    只读对写错的情况很隐蔽——用户切到 B 会话聊了半小时，
    下次 ``--continue`` 却找不到那些话（它们全写进了 A），
    而当下一切看起来都正常。
    """
    root = tmp_path / "sessions"
    await _write_session(root, ALPHA, [_text("甲的回答")])
    await _write_session(root, BETA, [_text("乙的回答")])

    alpha_path = root / f"{ALPHA}{SESSION_SUFFIX}"
    beta_path = root / f"{BETA}{SESSION_SUFFIX}"
    alpha_before = alpha_path.read_text(encoding="utf-8")
    beta_before = beta_path.read_text(encoding="utf-8")

    manager = _manager(tmp_path, session_id=ALPHA, rounds=[_text("切完之后的回答")])
    _feed(monkeypatch, [f"/switch {BETA}", "切完之后说的话", "exit"])
    assert await run_repl(manager) == 0

    beta_after = beta_path.read_text(encoding="utf-8")
    assert "切完之后说的话" in beta_after, "新的话没写进被切换到的会话文件"
    assert "切完之后的回答" in beta_after, "模型的回答也没写进去"
    # 旧文件**一个字节都不该多**——多了就说明写错了地方
    assert alpha_path.read_text(encoding="utf-8") == alpha_before, (
        "切换之后的话写进了旧会话文件——历史与文件已错位"
    )
    assert BETA in beta_after and beta_before in beta_after


@pytest.mark.asyncio
async def test_new_opens_a_fresh_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``/new`` 立即生效：新 id、空历史、**且新的话写进新文件**。

    "空历史"与"写进新文件"要一起断言：只断言 id 变了会放过
    "id 换了但树还是旧的"（那会导致新会话里能看到上一个会话的全部内容）。
    """
    root = tmp_path / "sessions"
    await _write_session(root, ALPHA, [_text("甲的回答")])

    manager = _manager(tmp_path, session_id=ALPHA, rounds=[_text("新会话的回答")])
    _feed(monkeypatch, ["/new", "新会话的第一句话", "exit"])
    assert await run_repl(manager) == 0

    out = capsys.readouterr().out
    new_id = manager.current_id
    assert new_id != ALPHA, "/new 没换 id"
    assert new_id in out, "新 id 没告诉用户——他接下来没法用 id 引用它"
    # 历史里**只有新会话自己那两条**（user + assistant）。
    # 断言"等于两轮"而不是"等于空"：后者在发过话之后本就不该成立，
    # 而它会把"旧历史被带过来了"这个真问题掩过去——所以要看**内容**。
    dumped = "".join(
        m.model_dump_json() for m in manager.current.context.build_messages()
    )
    assert f"{ALPHA} 里的任务" not in dumped, "新会话里带着旧会话的历史"
    assert "甲的回答" not in dumped, "新会话里带着旧会话的回答"

    assert (root / f"{new_id}{SESSION_SUFFIX}").exists(), "新会话没落盘"
    assert (root / f"{new_id}{SESSION_SUFFIX}").read_text(
        encoding="utf-8"
    ).count("新会话的第一句话") == 1


@pytest.mark.asyncio
async def test_unknown_command_errors_and_never_reaches_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**未知 ``/`` 命令报错 + 打帮助，绝不静默发给模型**（详规的显式行为变更）。

    这是本批最容易写错、后果也最坏的一条：把 ``/hlep`` 当任务发给模型，
    模型会**很配合地假装自己是命令行**，回一段格式漂亮的"帮助"——
    用户于是以为命令生效了，实际上它已经把一个无意义任务当成对话内容
    写进了会话历史，而这段垃圾会一直留在上下文里。

    断言方式是**数 transcript 轮次**：``FakeProvider`` 的 transcript 只有一轮，
    命令若被发给模型就会消费掉它，于是末轮断言失败（或抛 ``TranscriptExhausted``）。
    """
    manager = _manager(tmp_path, rounds=[_text("模型不该看到这条")])
    provider = manager._provider  # type: ignore[attr-defined]
    _feed(monkeypatch, ["/hlep", "exit"])

    assert await run_repl(manager) == 0

    out = capsys.readouterr().out
    assert "未知命令" in out and "/hlep" in out
    assert "/sessions" in out, "未知命令时没打帮助——用户不知道有哪些命令"
    assert provider.remaining_rounds == 1, (
        "命令被发给了模型：transcript 被消费掉了。"
        "模型会假装自己是命令行，把垃圾写进会话历史"
    )
    assert manager.current.history() == [], "命令进了会话历史"
    assert "模型不该看到这条" not in out, "把模型的回答打出来了——说明真发了"


@pytest.mark.asyncio
async def test_help_lists_all_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``/help`` 列出的命令**必须都能用**。

    这条防的是"帮助里的清单与 ``COMMANDS`` 表漂移"——那份清单一旦过期，
    用户按它敲命令会得到"未知命令"，然后他就不再相信任何文档了。
    """
    from sigma.repl import COMMANDS

    manager = _manager(tmp_path)
    _feed(monkeypatch, ["/help", "exit"])
    assert await run_repl(manager) == 0

    out = capsys.readouterr().out
    # 主名（别名不逐个断言：清单给的是"我能做什么"，不是"别名全集"）
    for name in ("sessions", "switch", "new", "help"):
        assert name in out, f"帮助里没列 {name}"
        assert name in COMMANDS, f"帮助里列了 {name}，但命令表里没有"


# ---------------------------------------------------------------------------
# F 组（2026-09-24 review）：--sub-agent 下的会话切换 / 裸命令 / 分派容错
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_roundtrip_with_sub_agent(tmp_path: Path) -> None:
    """``--sub-agent`` 下 /new → /switch 往返**不炸**，且两个会话的 task 工具独立。

    修复前：``SessionManager`` 的所有会话共享同一个 registry，
    ``InteractiveSession`` 在 ``enable_sub_agent=True`` 时往里注册 TaskTool——
    第二次 ``_build`` 必撞 ``DuplicateToolError``，REPL 当场终结。
    （``load_skill`` 有"已存在则跳过"，task 没有——两处不一致，取的是
    "每会话一份克隆注册表"而不是"改成跳过"：共享一个 TaskTool 实例
    会让两个会话的**信箱状态串味**，那是引入新 bug 的修法。）
    """
    root = tmp_path / "sessions"
    await _write_session(root, BETA, [_text("乙的回答")])

    manager = _manager(tmp_path, session_id=ALPHA, argv=["--sub-agent"])
    task_first = manager.current._registry.get("task")  # type: ignore[attr-defined]

    manager.new()
    task_new = manager.current._registry.get("task")  # type: ignore[attr-defined]
    outcome = manager.switch_to(BETA)
    task_switched = manager.current._registry.get("task")  # type: ignore[attr-defined]

    assert outcome.ok and outcome.switched
    # 三个会话各持一个**独立的** TaskTool 实例——信箱、在跑任务都随会话走
    assert task_first is not task_new
    assert task_new is not task_switched


@pytest.mark.asyncio
async def test_bare_switch_prints_usage_and_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """裸敲 ``/switch`` → **打用法提示**，REPL 循环继续。

    修复前：分派按"argument 是否为空"数着传参，``_cmd_switch`` 少收一个
    位置参数当场 ``TypeError``，且异常从分派一路穿出 REPL——**终结整个会话**。
    ``_cmd_switch`` 里那个空参提示分支成了死代码。
    """
    manager = _manager(tmp_path, session_id=ALPHA)
    _feed(monkeypatch, ["/switch", "exit"])
    assert await run_repl(manager) == 0

    out = capsys.readouterr().out
    assert "用法：/switch <序号|id>" in out
    assert manager.current_id == ALPHA, "裸 /switch 不该换会话"


@pytest.mark.asyncio
async def test_handler_exception_does_not_kill_repl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """命令 handler 抛异常 → **打印错误、REPL 不退出**，下一条命令照常工作。

    与 send 的"错误要打印，但不要终结会话"是同一条纪律——分派层此前不设防。
    """
    from sigma.repl import COMMANDS

    async def exploding(
        manager: SessionManager, index_map: dict[int, str], argument: str
    ) -> None:
        raise RuntimeError("命令内部炸了")

    COMMANDS["boom"] = (exploding, "")
    try:
        manager = _manager(tmp_path, session_id=ALPHA, rounds=[_text("好的")])
        _feed(monkeypatch, ["/boom", "/new", "exit"])
        assert await run_repl(manager) == 0
    finally:
        del COMMANDS["boom"]

    out = capsys.readouterr().out
    assert "命令失败" in out and "RuntimeError" in out
    # 后续命令照常执行：/new 真的换了会话
    assert manager.current_id != ALPHA
