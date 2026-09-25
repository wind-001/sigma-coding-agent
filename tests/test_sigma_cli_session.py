"""会话接续（``sessions.py`` + CLI ``--session`` / ``--continue``）的门禁测试。

对应 ``docs/plans/P2-会话树与上下文-详规.md`` 第 5 节门槛 **G61 / G62**
与第 10 节第 5 步。

**两组断言最要紧**

1. **``--continue`` 必须真的把历史读回来**（G61）。接续的全部意义就是
   "模型还记得之前说过什么"；若只接了个 id 而没读历史，
   症状是"模型忘了"——**看起来只是这次答得不好**，不像 bug。

2. **两个会话 id 绝不能串味**（G62）。串味的症状极隐蔽：
   你在 A 会话里说的话出现在 B 会话的上下文里。本项目的"会话 id → 文件名"
   有一次净化（``JsonlStore.path`` 把 ``/`` ``\\`` 换成 ``_``），
   而净化一旦退化成"所有 id 都映射到同一个文件"，两个会话就并到一处了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sigma import sdk
from sigma.cli import (
    DEFAULT_SESSIONS_DIR,
    SessionBinding,
    build_parser,
    resolve_session,
)
from sigma_ai.fake import FakeProvider
from sigma_session.sessions import (
    SESSION_SUFFIX,
    latest_session_id,
    list_sessions,
    new_session_id,
    session_path,
    session_previews,
)
from sigma_session.store import JsonlStore
from sigma_session.tree import SessionTree

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _args(*argv: str):  # type: ignore[no-untyped-def]
    """解析一个与真实调用一致的 Namespace（不用手搓，免得字段对不上）。"""
    return build_parser().parse_args(list(argv))


def _text(text: str) -> list[dict[str, object]]:
    return [
        {"type": "text_delta", "text": text},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _session(root: Path, session_id: str, rounds: list[list[dict[str, object]]]):  # type: ignore[no-untyped-def]
    """在 ``root`` 下开一个绑了存储的会话。"""
    return sdk.InteractiveSession(
        provider=FakeProvider.from_rounds(rounds),
        workspace_root=root,
        model="fake",
        session_id=session_id,
        tree=SessionTree.from_store(JsonlStore(root, session_id)),
        project_instructions="",
    )


def test_session_marks_baseline_checkpoint(tmp_path: Path) -> None:
    """会话启动时打**基线**快照（G65）。

    少了它，第一个写批次前的快照就是"已经被改过的状态"——
    第一次回滚无点可退，而用户会以为"回滚没生效"。
    """
    session = sdk.InteractiveSession(
        provider=FakeProvider.from_rounds([_text("嗯")]),
        workspace_root=tmp_path,
        model="fake",
        session_id="s1",
        project_instructions="",
        shadow_git_dir=tmp_path / "sessions" / "s1.shadow.git",
    )

    assert session.checkpoint is not None
    labels = [info.label for info in session.checkpoint.refs()]
    assert labels == ["baseline"]


def test_session_without_shadow_dir_has_no_checkpoint(tmp_path: Path) -> None:
    """不传影子库路径 = 没有 checkpoint（回放与既有测试路径的默认）。

    ``--no-checkpoint`` 走的也是这条路：**默认开、可关**，而不是默认关。
    """
    session = sdk.InteractiveSession(
        provider=FakeProvider.from_rounds([_text("嗯")]),
        workspace_root=tmp_path,
        model="fake",
        session_id="s2",
        project_instructions="",
    )

    assert session.checkpoint is None


def test_cli_shadow_dir_is_next_to_session_file(tmp_path: Path) -> None:
    """影子库与会话文件同层（``<sessions>/<id>.shadow.git``）。

    同层的意义：``--continue`` 续上会话就天然续上 checkpoint 历史；
    换一个目录会让"续了会话却回滚不到刚才那一步"。
    """
    from sigma.cli import shadow_git_dir_for

    assert shadow_git_dir_for(tmp_path, "会话 A") == tmp_path / "会话 A.shadow.git"
    assert shadow_git_dir_for(tmp_path, "a/b") == tmp_path / "a_b.shadow.git"


# ---------------------------------------------------------------------------
# sessions.py：目录操作
# ---------------------------------------------------------------------------


def test_new_session_id_is_sortable_and_unique() -> None:
    """id 形如 ``YYYYMMDD-HHMMSS.mmm-xxxx``：**能一眼看出先后**。

    与节点 id 取 uuid4 的理由不同——会话 id 是给人看的
    （``--continue`` 之后用户要知道自己续的是哪个）。

    **时间到毫秒**（2026-09-22 星辰要求）。末尾四位随机**仍然保留**：
    毫秒不保证唯一（同一毫秒内开两个会话是可能的），
    而撞了会写到同一个会话文件、两个会话的历史混在一起且不报错。
    """
    first = new_session_id(clock=lambda: 1_750_000_000)
    second = new_session_id(clock=lambda: 1_750_000_000)

    # YYYYMMDD(8) - HHMMSS(6) . mmm(3) - xxxx(4)
    assert len(first) == 8 + 1 + 6 + 1 + 3 + 1 + 4
    assert first[:8].isdigit() and first[9:15].isdigit()
    assert first[15] == "." and first[16:19].isdigit(), "毫秒位缺失"
    # 同一毫秒内也要不撞（脚本里连着调两次是可发生的）
    assert first != second


def test_list_sessions_on_missing_dir_is_empty(tmp_path: Path) -> None:
    """目录不存在 → 空列表，**不是错误**。一次都没跑过就没有会话。"""
    assert list_sessions(tmp_path / "从来没建过") == []
    assert latest_session_id(tmp_path / "从来没建过") is None


def test_list_sessions_orders_by_modified_desc(tmp_path: Path) -> None:
    """按修改时间倒序——``--continue`` 要的就是"最近那个"。"""
    old = JsonlStore(tmp_path, "旧的")
    new = JsonlStore(tmp_path, "新的")
    old.append([])  # 触发目录创建
    _write(tmp_path / f"旧的{SESSION_SUFFIX}", 1_000_000)
    _write(tmp_path / f"新的{SESSION_SUFFIX}", 2_000_000)

    assert [info.id for info in list_sessions(tmp_path)] == ["新的", "旧的"]
    assert latest_session_id(tmp_path) == "新的"


def test_list_sessions_ignores_non_jsonl(tmp_path: Path) -> None:
    """只认 ``.jsonl``——目录里可能有别的文件（README、临时文件）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _write(tmp_path / f"会话{SESSION_SUFFIX}", 1_000)
    (tmp_path / "笔记.md").write_text("x", encoding="utf-8")
    (tmp_path / "子目录").mkdir()

    assert [info.id for info in list_sessions(tmp_path)] == ["会话"]


def test_session_path_matches_store_convention(tmp_path: Path) -> None:
    """``session_path`` 与 ``JsonlStore.path`` **必须一致**。

    两者一旦漂移，症状是"``--continue`` 找不到刚存下的会话"，
    看起来像文件没写成功。这条断言把两份实现钉在一起。
    """
    assert session_path(tmp_path, "abc") == JsonlStore(tmp_path, "abc").path
    # 净化规则也一并钉住（``/`` 会被换成 ``_``）
    assert session_path(tmp_path, "a/b") == tmp_path / "a_b.jsonl"


def _write(path: Path, mtime: float) -> None:
    """写一个空会话文件并设定 mtime（构造"哪个更新"的场景）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    import os

    os.utime(path, (mtime, mtime))


# ---------------------------------------------------------------------------
# resolve_session：四种输入
# ---------------------------------------------------------------------------


def test_resolve_without_flags_creates_a_new_session(tmp_path: Path) -> None:
    binding = resolve_session(_args(), tmp_path)

    assert isinstance(binding, SessionBinding)
    assert binding.resumed is False
    assert binding.previous_messages == 0
    assert binding.session_id  # 自动生成


def test_resolve_session_creates_named_session(tmp_path: Path) -> None:
    """``--session ID``：不存在就建，且**不是"已续接"**。"""
    binding = resolve_session(_args("--session", "我的会话"), tmp_path)

    assert binding.session_id == "我的会话"
    assert binding.resumed is False


def test_resolve_session_reuses_existing(tmp_path: Path) -> None:
    """``--session ID`` 指向已存在的会话 → 读回历史（G61 的解析层）。"""
    session = _session(tmp_path, "已有", [_text("回答")])

    import asyncio

    asyncio.run(session.send("任务"))

    binding = resolve_session(_args("--session", "已有"), tmp_path)

    assert binding.session_id == "已有"
    assert binding.resumed is True
    assert binding.previous_messages == 2  # 上一轮 user + assistant


def test_continue_picks_the_latest(tmp_path: Path) -> None:
    """``--continue`` 取最近修改的那个。"""
    _write(tmp_path / f"早的{SESSION_SUFFIX}", 1_000_000)
    _write(tmp_path / f"晚的{SESSION_SUFFIX}", 3_000_000)

    binding = resolve_session(_args("--continue"), tmp_path)

    assert binding.session_id == "晚的"
    assert binding.resumed is True


def test_continue_with_nothing_to_resume_makes_a_new_one(tmp_path: Path) -> None:
    """``--continue`` 但目录是空的 → **开新的**，``resumed=False``。

    这个标记存在的意义：CLI 要据此打出"没有可续的会话"，而不是
    静默开一个新的——静默会让用户以为自己在续上下文，
    于是把模型的"答得不好"当成能力问题，而不是"上下文没接上"。
    """
    binding = resolve_session(_args("--continue"), tmp_path)

    assert binding.resumed is False
    assert binding.previous_messages == 0


def test_default_sessions_dir_is_under_user_config() -> None:
    """默认落点在**用户级配置目录**，不是工作区。

    往工作区里写 ``.sigma/`` 会污染别人的仓库——那个项目未必
    gitignore 它。判据与"工具层不猜密钥路径"同源（谁决定策略谁传参）。
    """
    assert DEFAULT_SESSIONS_DIR.name == "sessions"
    assert DEFAULT_SESSIONS_DIR.parent.name == ".sigma"
    assert "Desktop" not in str(DEFAULT_SESSIONS_DIR)  # 不是仓库相对路径


def test_continue_and_session_are_mutually_exclusive() -> None:
    """两个开关互斥，**在启动时报错**而不是静默让后者覆盖前者。"""
    import pytest

    with pytest.raises(SystemExit):
        _args("--continue", "--session", "x")


# ---------------------------------------------------------------------------
# G61：续接真的恢复历史（端到端）
# ---------------------------------------------------------------------------


async def test_resumed_session_sees_previous_history(tmp_path: Path) -> None:
    """**G61 本体**：重启之后模型拿到的消息里**真的有上一轮的内容**。

    用"两次独立构造 ``InteractiveSession``"来模拟进程重启——
    这正是真实场景：``sigma --continue`` 是**另一个进程**。
    若只接了个 id 而没读盘，第二次的 ``build_messages`` 里就只有系统消息，
    而它会**照样跑出一个回答**（模型不看历史也能答），所以必须断言消息内容。
    """
    first = _session(tmp_path, "续接", [_text("第一轮回答")])
    await first.send("把 a.py 改成异步")

    # 模拟进程重启：全新的 session 对象，只共享磁盘上的会话文件
    second = _session(tmp_path, "续接", [_text("第二轮回答")])
    # 断言方式：把组装出来的消息**整体序列化**再找文本。
    # 不逐个读 ``.message.content``——那是 agent 层字段，而
    # ``AssistantMessage.content`` 是**块列表**不是字符串
    # （写这条时先踩了一次：拿到 `[TextBlock(text=...)]` 然后断言字符串不在里面）。
    # 序列化后的整体文本才是"模型实际看到的东西"。agent 层消息与
    # ``CompactionSummary`` 都有 ``model_dump_json``，所以这个写法对类型不敏感。
    dumped = "".join(m.model_dump_json() for m in second.context.build_messages())

    assert "把 a.py 改成异步" in dumped, "上一轮的用户消息没有恢复"
    assert "第一轮回答" in dumped, "上一轮的助手回答没有恢复"


async def test_resumed_session_appends_after_previous_history(tmp_path: Path) -> None:
    """续跑之后历史**继续增长**，不是把旧的挤掉。"""
    first = _session(tmp_path, "续接2", [_text("一")])
    await first.send("任务一")

    second = _session(tmp_path, "续接2", [_text("二")])
    await second.send("任务二")

    assert len(second.history()) == 4  # 两轮 × (user + assistant)
    # 而磁盘上也确实是 4 条 —— 说明落盘与内存一致
    assert len(JsonlStore(tmp_path, "续接2").load().records) == 4


async def test_new_session_does_not_see_another_sessions_history(tmp_path: Path) -> None:
    """**G62 的另一面**：不同 id 的会话互不可见。

    与 G61 是一对——只有"续对了"而没有"不串味"，重启后仍会看到别人的历史。
    """
    other = _session(tmp_path, "甲的会话", [_text("甲的回答")])
    await other.send("甲的任务")

    fresh = _session(tmp_path, "乙的会话", [_text("乙的回答")])
    dumped = "".join(m.model_dump_json() for m in fresh.context.build_messages())

    assert "甲的任务" not in dumped
    assert len(fresh.history()) == 0


def test_two_sessions_get_two_files(tmp_path: Path) -> None:
    """**G62 本体**：两个 id → 两个文件。

    注入实验就是把这条路走窄（让净化把不同 id 映射到同一个文件名），
    那时两个会话会并到一处，历史互相污染。
    """
    first = _session(tmp_path, "甲", [_text("a")])
    second = _session(tmp_path, "乙", [_text("b")])

    import asyncio

    asyncio.run(first.send("甲的任务"))
    asyncio.run(second.send("乙的任务"))

    paths = {info.path for info in list_sessions(tmp_path)}
    assert len(paths) == 2
    # 而且各自的内容互不包含
    first_text = (tmp_path / "甲.jsonl").read_text(encoding="utf-8")
    second_text = (tmp_path / "乙.jsonl").read_text(encoding="utf-8")
    assert "甲的任务" in first_text and "甲的任务" not in second_text
    assert "乙的任务" in second_text and "乙的任务" not in first_text


# ---------------------------------------------------------------------------
# P5：会话列表的可辨认性（session_previews）
# ---------------------------------------------------------------------------


def test_preview_shows_first_user_message(tmp_path: Path) -> None:
    """``SessionPreview`` 必须能取出**首条用户消息**。

    这是 ``/sessions`` 可用性的全部：会话 id 是自动生成的
    （``20260923-193929.812-a1b2``），光看一串数字认不出"这是我哪次对话"。

    **这条测试曾经抓到过一个真 bug**：第一版 ``_first_user_text``
    只挖了一层 dict（直接找 ``content``），而磁盘上的记录是
    ``{message: {message: {...}}}`` 两层嵌套——于是**每条摘要都是 None**，
    ``/sessions`` 全显示"（还没有用户消息）"。那个症状看起来像
    "这功能还没做"，不像坏了。所以要断言**内容**，不能只断言"不为 None"。
    """
    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    session = _session(root, "有话的", [_text("回答")])
    import asyncio

    asyncio.run(session.send("帮我看看 read 工具的实现"))

    previews = session_previews(root)

    assert len(previews) == 1
    assert previews[0].first_user_text == "帮我看看 read 工具的实现"


def test_preview_truncates_long_first_message(tmp_path: Path) -> None:
    """超长首条消息**截断到 40 字 + …**：一行放得下才叫列表。"""
    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    session = _session(root, "长话", [_text("回答")])
    import asyncio

    asyncio.run(session.send("字" * 100))

    preview = session_previews(root)[0]

    assert preview.first_user_text is not None
    assert preview.first_user_text.endswith("…")
    # 40 个"字" + 省略号
    assert len(preview.first_user_text) == 41


def test_preview_skips_broken_file(tmp_path: Path) -> None:
    """**坏文件跳过，不拖垮整个列表**（与 ``list_sessions`` / ``load`` 同源）。

    会话目录里的文件是**用户数据**——一个写了一半的文件（进程被 kill）
    不该让 ``/sessions`` 整个报错。代价只是少显示一行。

    同时钉住"坏行跳过"：好行在坏行**后面**也要读到，
    否则"从第一行往后读、遇到坏的停下"这种写法会通过。
    """
    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    (root / "坏.jsonl").write_text(
        "这不是 JSON\n"
        '{"id": "n1", "parent_id": null, "message": {"timestamp": "t", "role": "llm",'
        ' "message": {"role": "user", "content": "坏文件里的好行", "timestamp": "t"}}}\n',
        encoding="utf-8",
    )
    session = _session(root, "好", [_text("答")])
    import asyncio

    asyncio.run(session.send("正常会话的首条"))

    by_id = {preview.id: preview for preview in session_previews(root)}

    assert set(by_id) == {"坏", "好"}, "坏文件把整个列表拖垮了"
    assert by_id["坏"].message_count == 1, "坏行没有被跳过"
    assert by_id["坏"].first_user_text == "坏文件里的好行"
    assert by_id["好"].first_user_text == "正常会话的首条"


def test_preview_compresses_whitespace(tmp_path: Path) -> None:
    """多行 / 多空格的输入压成一行：摘要里带换行会把列表打散。"""
    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    session = _session(root, "多行", [_text("答")])
    import asyncio

    asyncio.run(session.send("第一行\n第二行\t缩进"))

    assert session_previews(root)[0].first_user_text == "第一行 第二行 缩进"


def test_preview_limit_truncates_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit`` **先截断再读盘** —— 读 100 个文件再丢掉 80 个是纯浪费。

    断言方式是**数读盘次数**（monkeypatch ``open`` 太糙，改为数
    ``_preview_one`` 被调几次）。它同时钉住"列表顺序沿用 ``list_sessions``"：
    截断发生在那份倒序列表上，所以留下的必须是**最新的**几个。
    """
    from sigma_session import sessions as sessions_module

    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        _write(root / f"会话{index}{SESSION_SUFFIX}", 1_000_000 + index)

    calls: list[str] = []
    real = sessions_module._preview_one

    def counting(info, *, max_records):  # type: ignore[no-untyped-def]
        calls.append(info.id)
        return real(info, max_records=max_records)

    monkeypatch.setattr(sessions_module, "_preview_one", counting)

    previews = session_previews(root, limit=2)

    assert [p.id for p in previews] == ["会话4", "会话3"], "留下的不是最新的几个"
    assert calls == ["会话4", "会话3"], (
        f"读了不该读的文件：{calls}——截断必须发生在读盘之前"
    )


# ---------------------------------------------------------------------------
# P5：SessionManager（组装与切换）
# ---------------------------------------------------------------------------


def _manager(tmp_path: Path, *, session_id: str, sessions_root: Path | None = None):  # type: ignore[no-untyped-def]
    """造一个离线 manager（provider 是 ``FakeProvider``，不碰网络）。"""
    from sigma.cli import SessionBinding, SessionManager

    root = sessions_root if sessions_root is not None else tmp_path / "sessions"
    return SessionManager(
        args=_args(),
        workspace=tmp_path,
        base_url="http://fake",
        model="fake",
        api_key="sk-test",
        registry=__import__("sigma_agent.registry", fromlist=["ToolRegistry"]).ToolRegistry(),
        system_prompt="",
        sessions_root=root,
        binding=SessionBinding(session_id, SessionTree(), resumed=False, previous_messages=0),
        shadow_git_dir=None,
        skills_root=None,
        make_provider=lambda: FakeProvider.from_rounds([_text("好")]),
    )


def test_manager_switch_loads_history_into_the_new_session(tmp_path: Path) -> None:
    """切换后**新会话的 context 里真的有旧内容**。

    这与 ``/sessions`` 摘要、与 G61 都不同：这里测的是
    ``SessionManager`` 这一个组件的契约——它必须 ``from_store``。
    少了那一步的后果在 REPL 那一侧才看得见（G87），
    而在这一层就钉住，坏掉时能立刻指到是哪一步少做了。
    """
    import asyncio

    root = tmp_path / "sessions"
    other = _session(root, "另一个", [_text("旧回答")])
    asyncio.run(other.send("旧任务"))

    manager = _manager(tmp_path, session_id="当前")

    outcome = manager.switch_to("另一个")

    assert outcome.ok and outcome.switched
    assert outcome.messages == 2
    dumped = "".join(
        m.model_dump_json() for m in manager.current.context.build_messages()
    )
    assert "旧任务" in dumped and "旧回答" in dumped


def test_manager_switch_to_missing_file_is_not_ok(tmp_path: Path) -> None:
    """不存在的 id → ``ok=False``，且**当前会话不动**。

    "失败时把当前会话换掉"是最坏的失败形态：用户以为自己在旧上下文里，
    实际拿到了一个空的。所以失败路径**必须**不产生副作用。
    """
    manager = _manager(tmp_path, session_id="当前")

    outcome = manager.switch_to("查无此会话")

    assert outcome.ok is False
    assert manager.current_id == "当前", "切换失败却改了当前会话"


def test_manager_switch_to_self_reloads_without_switching(tmp_path: Path) -> None:
    """切到当前会话：``ok=True`` 但 ``switched=False`` —— **不是错误**。

    用户这么敲通常是想"重来一遍"（把内存里未落盘的改动丢掉）。
    报错会让他以为命令拼错了。

    这里手工建一个**存在的**空文件（不用 ``JsonlStore.append([])``——
    它对空列表直接 return，不建文件，于是测试会因为"文件不存在"而红，
    而那是测试自己的构造错误，不是被测量的问题）。
    """
    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"当前{SESSION_SUFFIX}").write_text("", encoding="utf-8")
    manager = _manager(tmp_path, session_id="当前")

    outcome = manager.switch_to("当前")

    assert outcome.ok is True
    assert outcome.switched is False


def test_manager_switch_accepts_file_suffix(tmp_path: Path) -> None:
    """``/switch abc.jsonl`` 与 ``/switch abc`` 是同一个会话。

    用户在 ``/sessions`` 里看到的是 id，从资源管理器里看到的是文件名——
    两种写法都得认，否则他要手工删掉后缀，而那不是他能预期的事。
    """
    import asyncio

    root = tmp_path / "sessions"
    asyncio.run(_session(root, "有后缀", [_text("答")]).send("任务"))
    manager = _manager(tmp_path, session_id="当前")

    outcome = manager.switch_to("有后缀.jsonl")

    assert outcome.ok is True
    assert manager.current_id == "有后缀"


def test_manager_switch_rejects_path_traversal(tmp_path: Path) -> None:
    """``../../`` 这类 id **不能探测会话目录之外的文件**。

    ``switch_to`` 的第一件事是 ``path.is_file()``——那已经是一次
    越界的存在性探测。净化发生在拼路径**之前**，所以探测范围被钉在目录内。

    这条不能只断言"切换失败"：失败是必然的（外面没有那个会话），
    要断言的是**探测没有越界**——用一个确实存在于目录外的文件来证明。
    """
    outside = tmp_path / "机密.txt"
    outside.write_text("不该被看到", encoding="utf-8")
    manager = _manager(tmp_path, session_id="当前")

    outcome = manager.switch_to("../机密.txt")

    assert outcome.ok is False, "越界路径被当成了合法会话"
    # 净化后的 id 只应保留目录内的那一段，绝不能含 ..
    assert ".." not in outcome.session_id


# ---------------------------------------------------------------------------
# F3 / F4（2026-09-24 review）：--continue 空目录要落盘 / 坏字节行可跳过
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_on_empty_dir_persists_new_session(tmp_path: Path) -> None:
    """``--continue`` 遇到空目录 → 开的新会话**必须落盘**。

    修复前这条分支返回 ``SessionTree()``——纯内存树，会话全程不写盘，
    退出后 ``--continue`` 永远找不到它。症状是"我刚才明明跑过一段对话"，
    而磁盘上什么都没有（2026-09-24 review 修复）。
    """
    args = _args("--continue")
    binding = resolve_session(args, tmp_path)
    assert binding.resumed is False

    session = sdk.InteractiveSession(
        provider=FakeProvider.from_rounds([_text("好的")]),
        workspace_root=tmp_path,
        model="fake",
        session_id=binding.session_id,
        tree=binding.tree,
        project_instructions="",
    )
    await session.send("空目录上的第一句话")

    path = tmp_path / f"{binding.session_id}{SESSION_SUFFIX}"
    assert path.exists(), "--continue 空目录分支起的会话没落盘"
    assert "空目录上的第一句话" in path.read_text(encoding="utf-8")
