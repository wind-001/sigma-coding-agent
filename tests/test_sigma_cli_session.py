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
