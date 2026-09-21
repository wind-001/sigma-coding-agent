"""会话树与存储：``store.py`` / ``tree.py`` 的门禁测试。

对应 ``docs/plans/P2-会话树与上下文-详规.md`` 第 5 节门槛
G48（树路径正确）、G49（环检测）、G53（追加幂等）。

**这个文件里有一类断言特别重要：两条相反取舍的对照**

    ``store.load()`` 遇到坏行是 **跳过 + warning**，
    而 ``sigma_agent.messages_from_jsonl`` 遇到坏行是 **抛错 + 行号**。
    它们看起来"风格不一致"，实际是**两种数据上的两种正确处置**
    （用户数据要能救多少救多少 / 测试夹具坏了要立刻知道）。

    所以这里**两个都测**（``test_bad_line_is_skipped_*`` 与
    ``test_same_bad_line_raises_in_message_from_jsonl``）——
    万一后人把它"统一"掉，无论统一到哪一边，都会有一条测试红。
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
from sigma_agent.agent_messages import (
    LlmMessageWrapper,
    MessageDecodeError,
    messages_from_jsonl,
)
from sigma_ai.messages import UserMessage
from sigma_session.store import (
    BadLineSkipped,
    JsonlStore,
    LoadResult,
    NodeRecord,
    message_of,
    new_node_id,
    record_of,
)
from sigma_session.tree import (
    SessionTree,
    TreeCorrupted,
    UnknownNode,
    detect_corruption,
)

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


_COUNTER = [0]


def _msg(text: str) -> LlmMessageWrapper:
    """造一条可落盘的 agent 层消息。

    时间戳递增而不是固定值：固定值会让"两条消息完全相同"
    从而掩盖"顺序错了"这类缺陷（``model_dump`` 出来一模一样）。
    """
    _COUNTER[0] += 1
    stamp = _COUNTER[0]
    return LlmMessageWrapper(
        timestamp=stamp, message=UserMessage(content=text, timestamp=stamp)
    )


def _raw_line(
    node_id: str, parent_id: str | None, text: str
) -> dict[str, object]:
    """手写一行原始记录（用于构造损坏文件）。"""
    return {
        "id": node_id,
        "parent_id": parent_id,
        "message": record_of(_msg(text)).message,
    }


def _write_raw(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# store：基本读写
# ---------------------------------------------------------------------------


def test_append_then_load_roundtrip(tmp_path: Path) -> None:
    """写进去再读回来，字段一个不少。"""
    store = JsonlStore(tmp_path, "s1")
    store.append([record_of(_msg("a")), record_of(_msg("b"))])

    result = store.load()

    assert isinstance(result, LoadResult)
    assert result.clean
    assert [message_of(r).to_llm().content for r in result.records] == ["a", "b"]  # type: ignore[union-attr]


def test_load_missing_file_is_empty_not_error(tmp_path: Path) -> None:
    """文件不存在**不是错误**——新会话就是没有文件。

    若这里抛错，每个入口都要先 ``if not exists()``，而那是最容易漏的约定。
    """
    result = JsonlStore(tmp_path, "never-written").load()
    assert result.records == []
    assert result.skipped_lines == []
    assert result.clean


def test_one_session_one_file(tmp_path: Path) -> None:
    """一个会话一个文件（Q1）。两个会话互不干扰。"""
    first = JsonlStore(tmp_path, "s1")
    second = JsonlStore(tmp_path, "s2")
    first.append([record_of(_msg("one"))])
    second.append([record_of(_msg("two")), record_of(_msg("three"))])

    assert first.path != second.path
    assert len(first.load().records) == 1
    assert len(second.load().records) == 2


def test_session_id_path_separators_are_sanitized(tmp_path: Path) -> None:
    """``--session a/b`` 不能写到 root 之外。

    这不是安全边界（真边界属 P3 钩子），只是防止
    "``--session a/b`` 报 FileNotFoundError"这类困惑性失败。
    """
    store = JsonlStore(tmp_path, "a/b\\c")
    assert store.path.parent == tmp_path
    assert store.path.name == "a_b_c.jsonl"


def test_append_creates_root_directory(tmp_path: Path) -> None:
    """``append`` 建父目录——与 ``WriteTool`` 的"不建父目录"**相反**。

    判据是信任边界：本模块的 root 是配置项、由我们自己的代码驱动；
    ``WriteTool`` 的路径来自模型。两处都要按各自场景判，不要统一。
    """
    root = tmp_path / "deeply" / "nested"
    assert not root.exists()
    JsonlStore(root, "s1").append([record_of(_msg("x"))])
    assert root.is_dir()


def test_append_empty_list_writes_nothing(tmp_path: Path) -> None:
    """空列表不产生文件——避免留下一个 0 字节文件让后来者困惑。"""
    store = JsonlStore(tmp_path, "s1")
    store.append([])
    assert not store.path.exists()


def test_new_node_id_is_unique() -> None:
    """id 唯一（Q1 选 uuid4 的理由是多进程并发）。"""
    ids = {new_node_id() for _ in range(500)}
    assert len(ids) == 500


# ---------------------------------------------------------------------------
# store：坏行处置（与 messages_from_jsonl 的相反取舍）
# ---------------------------------------------------------------------------


def test_bad_line_is_skipped_and_reported(tmp_path: Path) -> None:
    """坏行跳过 + warning + **记下行号**，好的行照常读回。

    三样都要断言：少一个都会让"能救多少救多少"变成"悄悄地少几条消息"。
    """
    store = JsonlStore(tmp_path, "s1")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(
        {"id": "n1", "parent_id": None, "message": record_of(_msg("good")).message},
        ensure_ascii=False,
    )
    store.path.write_text(
        f"{good}\n{{这不是 JSON}}\n\n{good.replace('n1', 'n2')}\n", encoding="utf-8"
    )

    with pytest.warns(BadLineSkipped) as record:
        result = store.load()

    assert result.skipped_lines == [2]
    assert len(result.records) == 2
    # warning 必须带行号——否则它自己就成了新的"无从排查"
    assert "2" in str(record[0].message)


def test_line_with_wrong_shape_is_skipped(tmp_path: Path) -> None:
    """合法 JSON 但字段不对（缺 ``id``）同样算坏行。"""
    store = JsonlStore(tmp_path, "s1")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"parent_id": None, "message": {}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.warns(BadLineSkipped):
        result = store.load()

    assert result.records == []
    assert result.skipped_lines == [1]


def test_same_bad_line_raises_in_message_from_jsonl() -> None:
    """**对照组**：同一份坏数据，``messages_from_jsonl`` 必须**抛错**。

    这是上面那条"跳过"的镜像。两者都对——
    夹具坏了要立刻知道，用户数据坏了要能救多少救多少。
    **若有人把两者统一成任一种行为，这两条测试必有一条会红。**
    """
    with pytest.raises(MessageDecodeError):
        messages_from_jsonl("{这不是 JSON}")


# ---------------------------------------------------------------------------
# tree：追加与路径
# ---------------------------------------------------------------------------


def test_append_chains_onto_head(tmp_path: Path) -> None:
    """``append`` 默认接在当前 head 上，**不是变成新根**。

    这是最容易写错的一处：默认变成新根的话，每条消息都是孤立根，
    ``path_to`` 只返回长度 1 的历史——症状是"模型看不到之前说过的话"，
    而且**不报任何错**。
    """
    tree = SessionTree(store=JsonlStore(tmp_path, "s1"))
    first = tree.append(_msg("a"))
    second = tree.append(_msg("b"))

    assert tree.path_to(second) == [first, second]
    assert tree.roots() == [first]


def test_path_to_contains_only_own_branch(tmp_path: Path) -> None:
    """**G48 本体**：分支节点的路径**不含兄弟分支**。

    含了兄弟的后果：模型拿着另一条分支的历史做当前决策，
    而症状是"它提到了我没说过的事"——在长会话里极难定位。
    """
    tree = SessionTree(store=JsonlStore(tmp_path, "s1"))
    root = tree.append(_msg("root"))
    left = tree.append(_msg("left"))
    # 从 root 分叉出右支
    right = tree.append_to(_msg("right"), root)

    assert tree.path_to(left) == [root, left]
    assert tree.path_to(right) == [root, right]
    # 左支不受右支影响
    assert left not in tree.path_to(right)
    assert right not in tree.path_to(left)


def test_branch_and_rollback(tmp_path: Path) -> None:
    """分支 + 回滚：``set_head`` 挪回去之后，新消息接在那里。"""
    tree = SessionTree(store=JsonlStore(tmp_path, "s1"))
    root = tree.append(_msg("root"))
    first = tree.append(_msg("first"))
    tree.append(_msg("second"))

    tree.set_head(first)  # 回滚
    forked = tree.append(_msg("forked"))

    assert tree.path_to(forked) == [root, first, forked]
    assert tree.head_id == forked


def test_history_follows_head(tmp_path: Path) -> None:
    """``history`` 返回当前 head 分支的消息内容。"""
    tree = SessionTree(store=JsonlStore(tmp_path, "s1"))
    tree.append(_msg("one"))
    second = tree.append(_msg("two"))

    tree.set_head(second)
    assert [m.message.content for m in tree.history()] == ["one", "two"]  # type: ignore[union-attr]


def test_history_empty_when_no_nodes(tmp_path: Path) -> None:
    """空树的 ``history`` 是空列表，不是异常。"""
    assert SessionTree(store=JsonlStore(tmp_path, "s1")).history() == []


def test_reload_does_not_accumulate(tmp_path: Path) -> None:
    """**G53（修订版）**：重复 ``load()`` 不累加。

    计划里 G53 原本写的是「同一批消息重复 ``append`` 后不产生重复节点，
    注入方式 = 去掉去重/幂等键」。**那个前提在本设计里站不住**：

    1. 每条记录的 id 都是 uuid4，重复 ``append`` 得到的是**两个合法的不同节点**
       ——它们本来就是两条不同的消息，**不该被去重**；
    2. 若真按"去重"实现，症状是**用户连说两次「好」只留下一句**，
       而这是个静默的数据丢失。**为一个不存在的需求引入一个真缺陷，不划算。**

    所以门槛换成**真实存在的相邻属性**：``load()`` 是**替换**语义而不是**追加**语义。
    它的失败模式很实在——若把 ``self._raw = list(result.records)``
    写成 ``extend``，那么每次重载都会让节点数**翻倍**、``roots()`` 出现重复根，
    而**不会报任何错**。注入实验就是改成 ``extend``。
    """
    store = JsonlStore(tmp_path, "s1")
    tree = SessionTree(store=store)
    first = tree.append(_msg("a"))
    second = tree.append(_msg("b"))

    tree.load()
    tree.load()

    assert len(tree) == 2
    assert tree.path_to(second) == [first, second]
    assert tree.roots() == [first]


def test_unknown_node_raises(tmp_path: Path) -> None:
    """引用不存在的节点抛 ``UnknownNode``（``ValueError``）。"""
    tree = SessionTree(store=JsonlStore(tmp_path, "s1"))
    tree.append(_msg("a"))

    with pytest.raises(UnknownNode):
        tree.path_to("nope")
    with pytest.raises(UnknownNode):
        tree.append_to(_msg("x"), "nope")
    with pytest.raises(UnknownNode):
        tree.set_head("nope")


def test_append_to_uses_explicit_parent(tmp_path: Path) -> None:
    """``append_to`` 的父节点是**显式**的，不受 head 影响。"""
    tree = SessionTree(store=JsonlStore(tmp_path, "s1"))
    root = tree.append(_msg("root"))
    tail = tree.append(_msg("tail"))
    assert tree.head_id == tail

    # head 在 tail，但显式指到 root
    side = tree.append_to(_msg("side"), root)
    assert tree.path_to(side) == [root, side]


# ---------------------------------------------------------------------------
# tree：持久化
# ---------------------------------------------------------------------------


def test_reload_from_disk_reproduces_structure(tmp_path: Path) -> None:
    """重载后路径与 head 一致——这是"落盘真的能用"的唯一证据。"""
    store = JsonlStore(tmp_path, "s1")
    tree = SessionTree(store=store)
    root = tree.append(_msg("root"))
    leaf = tree.append(_msg("leaf"))
    side = tree.append_to(_msg("side"), root)

    reloaded = SessionTree.from_store(store)

    assert len(reloaded) == 3
    assert reloaded.path_to(leaf) == [root, leaf]
    assert reloaded.path_to(side) == [root, side]
    assert reloaded.head_id == side
    assert reloaded.clean


def test_loading_without_store_raises(tmp_path: Path) -> None:
    """没绑存储的树不能 ``load``。**报错而不是静默返回空**——
    静默返回空会让"忘了传 store"表现成"会话是空的"。"""
    with pytest.raises(TreeCorrupted):
        SessionTree().load()


def test_store_never_interprets_parent_id(tmp_path: Path) -> None:
    """**分层边界**：``store`` 原样搬运 ``parent_id``，不解释它。

    证据：把一条 ``parent_id`` 指向不存在节点的记录交给 store，
    它读回来时**原样保留**（不做任何修补）——
    结构解释是 ``tree`` 的职责。
    """
    store = JsonlStore(tmp_path, "s1")
    store.append([NodeRecord.model_validate(_raw_line("n1", "ghost", "x"))])

    records = store.load().records
    assert len(records) == 1
    assert records[0].parent_id == "ghost"


# ---------------------------------------------------------------------------
# tree：结构损坏（G49）
# ---------------------------------------------------------------------------


def test_cycle_is_detected_with_specific_chain() -> None:
    """**G49 本体**：环被检测到，且诊断里给出**具体的链**。

    为什么要断言"成环"这几个字而不是只断言"节点被标记为坏"：
    本实现有**两条防线**——``seen`` 集合给精确诊断，
    步数上限保证不挂死。若只有前者被写坏，后者仍会把节点标成坏，
    于是"节点坏了"这条断言依然会绿——**兜底不等于正确**。
    断言具体链条，才能让"防线一失效"这件事**红出来**。
    """
    records = [
        NodeRecord.model_validate(_raw_line("A", "B", "a")),
        NodeRecord.model_validate(_raw_line("B", "A", "b")),
    ]

    corrupt = detect_corruption(records)

    assert set(corrupt) == {"A", "B"}
    assert "成环" in corrupt["A"]
    # 链条要具体到能看出环在哪
    assert "->" in corrupt["A"]


def test_cycle_does_not_hang_and_healthy_branch_survives(tmp_path: Path) -> None:
    """环存在时：**不挂死**，且同一文件里的健康分支照常可用。

    这条同时钉住两件事：
    ① ``path_to`` 不会无限循环（G49 的行为面）；
    ② "能救多少救多少"是真的——一个分支坏了不该毁掉整个会话。
    """
    store = JsonlStore(tmp_path, "s1")
    _write_raw(
        store.path,
        [
            _raw_line("ROOT", None, "root"),
            _raw_line("C", "ROOT", "c"),
            _raw_line("A", "B", "a"),
            _raw_line("B", "A", "b"),
        ],
    )

    tree = SessionTree.from_store(store)

    # ① 健康分支可用
    assert tree.path_to("C") == ["ROOT", "C"]
    assert tree.head_id == "C"  # head 落在最后一个健康节点上
    # ② 坏节点被拒（且是**立刻**拒，不是死循环）
    with pytest.raises(TreeCorrupted):
        tree.path_to("A")
    with pytest.raises(TreeCorrupted):
        tree.append_to(_msg("x"), "A")


def test_dangling_parent_is_corrupt_and_propagates() -> None:
    """断链被标记，且**沿父子链传播**——祖先未知则后代的历史也是残缺的。"""
    records = [
        NodeRecord.model_validate(_raw_line("DANG", "MISSING", "d")),
        NodeRecord.model_validate(_raw_line("CHILD", "DANG", "c")),
        NodeRecord.model_validate(_raw_line("ROOT", None, "r")),
    ]

    corrupt = detect_corruption(records)

    assert set(corrupt) == {"DANG", "CHILD"}
    assert "不存在" in corrupt["DANG"]
    # 后代的原因来自祖先——它自己没有断链，但历史缺了一段
    assert corrupt["CHILD"] == corrupt["DANG"]


def test_duplicate_id_is_corrupt() -> None:
    """同一 id 出现在多行 → 损坏。**不猜哪条为准**（有歧义宁可报错）。"""
    records = [
        NodeRecord.model_validate(_raw_line("SAME", None, "first")),
        NodeRecord.model_validate(_raw_line("SAME", None, "second")),
    ]

    corrupt = detect_corruption(records)

    assert "SAME" in corrupt
    assert "重复" in corrupt["SAME"]


def test_healthy_tree_reports_no_corruption(tmp_path: Path) -> None:
    """对照：健康树 ``corrupt_nodes`` 为空、``clean`` 为真。

    没有这条，"检测器恒返回全坏"也能让上面几条测试通过。
    """
    tree = SessionTree(store=JsonlStore(tmp_path, "s1"))
    tree.append(_msg("a"))
    tree.append(_msg("b"))

    assert tree.corrupt_nodes() == {}
    assert tree.clean


def test_sibling_branches_share_ancestor_not_each_other(tmp_path: Path) -> None:
    """两条分支共享祖先，但``path_to`` 互不包含对方。

    这条比 ``test_path_to_contains_only_own_branch`` 更强：
    它验证"共享前缀 + 分叉"这个形状，而不只是"两条独立链"。
    """
    tree = SessionTree(store=JsonlStore(tmp_path, "s1"))
    root = tree.append(_msg("root"))
    common = tree.append(_msg("common"))
    left = tree.append(_msg("left"))
    right = tree.append_to(_msg("right"), common)

    assert tree.path_to(left) == [root, common, left]
    assert tree.path_to(right) == [root, common, right]
    assert tree.children(common) == [left, right]
