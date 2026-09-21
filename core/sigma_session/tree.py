"""会话树：在只追加的 JSONL 上做分支与回滚。

为什么是树而不是列表
    线性历史做不到"从这个点换个方向重来"。会话树加一个 ``parent_id``
    就得到了分支与回滚，而成本只有一列——这是架构 2.1 节说的
    "性价比最高的单项设计"。**成本极低、收益极大**，所以先做它。

一句话模型
    每个节点有一个 ``parent_id``（``None`` = 根）。从任一节点沿 ``parent_id``
    回溯到根，就是**那个分支的完整历史**。分支 = 追加时指向一个较早的节点；
    回滚 = 把 ``head`` 挪回某个祖先。

**结构损坏（环 / 断链）由本模块负责，不由 ``store.py`` 负责**

    存储层只做**行级**解析：一行不是合法 JSON 就跳过。
    而"``parent_id`` 指向不存在的节点"这类损坏，**每一行单独看都是合法的**——
    只有把整个图连起来看才暴露。

    所以本模块在加载后做一次**全图结构化检查**（``detect_corruption``），
    产出一份"哪些节点坏了、为什么"的报告（``corrupt_nodes()``）。
    坏的节点**不参与** ``path_to``，健康的节点**照常可用**——
    这兑现了 Q1 的"能救多少救多少"：一个分支坏了，不该让别的分支也读不出来。

**``path_to`` 为什么有两条防线（这不是冗余）**

    防线一：``seen`` 集合 + 精确的环诊断。它能告诉你
    "A -> B -> C -> A" 这条链具体是什么，而**精确的诊断信息本身有价值**。

    防线二：**步数上限 = 节点总数 + 1**。没有环时回溯链不可能超过节点总数，
    所以超了就必然是环。它保证"即便防线一被写坏，进程也不会挂死"。

    为什么非得有防线二：这是一个**只追加的文件**，
    而损坏可能来自手工编辑、磁盘错误、并发写。若只有防线一，
    它一旦失效（比如被重构掉），症状是**测试进程无限循环**——
    而挂死的 CI 比失败的 CI 难排查得多。**宁可慢一点报错，不要挂住。**

    由门槛 G49 钉住：``corrupt_nodes()`` 里必须出现**带具体链条的环诊断**，
    而不只是"链太长"。这样即便防线一被破坏、防线二兜住了结果，
    测试仍然会红——**兜底不等于正确**。
"""

from __future__ import annotations

from sigma_agent.agent_messages import AgentMessage

from sigma_session.store import (
    JsonlStore,
    LoadResult,
    NodeRecord,
    message_of,
    record_of,
)


class TreeCorrupted(RuntimeError):
    """**磁盘上的**会话数据结构性损坏（环 / 断链 / id 重复）。

    继承 ``RuntimeError`` 而不是 ``ValueError``：这不是"调用方传错了值"，
    而是"存下来的数据不成立"。两者的处置不同——
    前者要调用方改代码，后者要人来检查那个会话文件。
    """


class UnknownNode(ValueError):
    """调用方引用了一个不存在的节点。

    继承 ``ValueError``：这确实是"值非法"，且要调用方改代码。
    与 :class:`TreeCorrupted` 分开，是因为它们是**两个不同的时间点**
    （与 ``agent_messages`` 里三层异常家族同源：错误处理按阶段分策略）。
    """

    def __init__(self, node_id: str, known: list[str]) -> None:
        self.node_id = node_id
        super().__init__(
            f"节点 {node_id!r} 不存在。当前已知 {len(known)} 个节点："
            f"{sorted(known)[:10]}{' …' if len(known) > 10 else ''}"
        )


def detect_corruption(records: list[NodeRecord]) -> dict[str, str]:
    """扫描全部节点，返回 ``{节点 id: 损坏原因}``。

    健康的结构返回空 dict。

    判定分三类，**都只在这一处实现**（``path_to`` 依赖它的结论，
    不自己重新判一遍——两份实现意味着修一个忘另一个）：

    1. **id 重复**：同一个 id 出现在多行。只追加的文件本不该如此，
       出现即说明有人手工改过或并发写坏了。保留第一条，标记该 id 损坏
       （**不猜哪条为准**——"有歧义宁可报错不要猜"）。
    2. **断链**：``parent_id`` 指向一个不存在的节点。
       注意这常常是**上游坏行被跳过的后果**——但依然要标记，
       因为"祖先未知"意味着这条分支的上下文是**残缺的**，
       静默当成根节点会让模型看到一段"凭空开始"的历史。
    3. **成环**：沿 ``parent_id`` 回溯回到了走过的节点。

    损坏会**沿父子链传播**：祖先坏了，后代也算坏——
    因为它们的 ``path_to`` 必然包含那段未知的祖先。
    """
    by_id: dict[str, NodeRecord] = {}
    # 初始化 status：id 重复先在这里落一条
    status: dict[str, str] = {}

    for record in records:
        if record.id in by_id:
            status[record.id] = "节点 id 重复——同一 id 出现在多行，无法判断哪条为准"
            continue
        by_id[record.id] = record

    budget = len(by_id) + 1

    for start in by_id:
        if start in status:
            # 已经判定过（含上面的 id 重复）——不重复走一遍
            continue

        path: list[str] = []
        index: dict[str, int] = {}
        reason = ""
        cursor: str | None = start
        steps = 0

        while cursor is not None:
            steps += 1
            if steps > budget:
                # 防线二：不可能是"链恰好超长"，没有环时链长必然 ≤ 节点总数
                reason = (
                    f"parent_id 链在 {budget} 步内没有走到根"
                    f"（结构性损坏，超出了节点总数）"
                )
                break

            known = status.get(cursor)
            if known is not None:
                reason = known
                break

            if cursor in index:
                # 防线一：给出**具体的环**，这是诊断质量所在
                members = path[index[cursor] :]
                reason = f"parent_id 成环：{' -> '.join([*members, cursor])}"
                for member in members:
                    status[member] = reason
                break

            # 变量名刻意不叫 ``record``：外层 ``for record in records`` 已经占了
            # 那个名字，复用会让 mypy 把这里的 ``NodeRecord | None``
            # 判成与外层的 ``NodeRecord`` 冲突（也确实容易读混）。
            found = by_id.get(cursor)
            if found is None:
                reason = f"parent_id 指向不存在的节点 {cursor!r}（断链）"
                break

            index[cursor] = len(path)
            path.append(cursor)
            cursor = found.parent_id

        for node_id in path:
            if node_id not in status:
                status[node_id] = reason

    return {node_id: why for node_id, why in status.items() if why}


class SessionTree:
    """会话树。

    典型用法::

        tree = SessionTree.from_store(JsonlStore(root, "s1"))
        tree.append(user_message)                  # 接在当前 head 上
        tree.append(assistant_message)
        fork = tree.path_to(tree.head_id)[-3]      # 回退到某个祖先
        tree.append_to(other_message, fork)        # 从那里分叉

    **持久化是"顺带"的**：``append`` 先写盘再改内存，
    所以内存里的树**永远不领先于磁盘**——崩在中间只会丢一条尚未发生的操作，
    不会留下"内存以为写了、盘上没有"的状态。

    **为什么不抽 ``BaseStore``**（P2 详规 Q4）：本阶段只有 JSONL 一个后端，
    按项目已固化的判据（"只有一个子类的抽象基类 = 纯间接，要删"），
    现在抽它等于给唯一实现加一层纯间接。等第二个后端（SQLite / 内存替身）
    真的出现再抽，那时是一次 ``git mv`` + 提取，代价几乎为零。
    **架构 3.1 的目录树里写了 ``base.py # BaseStore（ABC）``，所以在这里记明：
    没做是决定，不是遗漏。**
    """

    def __init__(self, *, store: JsonlStore | None = None) -> None:
        self._store = store
        self._raw: list[NodeRecord] = []
        self._by_id: dict[str, NodeRecord] = {}
        self._children: dict[str, list[str]] = {}
        self._corrupt: dict[str, str] = {}
        self._head: str | None = None

    # ------------------------------------------------------------------
    # 构造与加载
    # ------------------------------------------------------------------

    @classmethod
    def from_store(cls, store: JsonlStore) -> SessionTree:
        tree = cls(store=store)
        tree.load()
        return tree

    def load(self) -> LoadResult:
        """从存储读入并重建索引。返回存储层的加载结果（含被跳过的行号）。

        **每次调用都整体重建**，而不是增量合并：增量合并要处理
        "磁盘上有别人写的新行"这类并发场景，而那是**没有验证过的需求**。
        整体重建是显然正确的，代价是 O(n)——会话规模下可以忽略。
        """
        if self._store is None:
            raise TreeCorrupted(
                "本 SessionTree 没有绑定存储（构造时 store=None），无法加载。"
                "内存模式请直接用 append()。"
            )
        result = self._store.load()
        self._raw = list(result.records)
        self._rebuild()
        return result

    def _rebuild(self) -> None:
        """由 ``_raw`` 重算全部派生状态。**唯一的重算入口。**

        集中在一处是为了让"内存状态与磁盘一致"这件事只有一个执行点——
        分散在多处的话，加一个操作就可能漏更新一处索引，
        而症状（某个查询返回旧结果）离根因很远。
        """
        by_id: dict[str, NodeRecord] = {}
        children: dict[str, list[str]] = {}
        for record in self._raw:
            by_id.setdefault(record.id, record)
            if record.parent_id is not None:
                children.setdefault(record.parent_id, []).append(record.id)

        self._by_id = by_id
        self._children = children
        self._corrupt = detect_corruption(self._raw)

        # head 落在"最后一个健康的节点"上。
        # 为什么不是"最后一条记录"：若最后一条恰好是坏节点，
        # 把 head 停在它上面会让下一次 append 立刻失败（父节点损坏）。
        if self._head is None or self._head not in by_id or self._head in self._corrupt:
            self._head = next(
                (
                    record.id
                    for record in reversed(self._raw)
                    if record.id not in self._corrupt
                ),
                None,
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(self, message: AgentMessage) -> str:
        """接在**当前 head** 之后追加，返回新节点 id。

        **默认接在 head 上，而不是变成新根**——这是最容易写错的一处：
        若默认 ``parent_id=None``，每条消息都会变成一个孤立的根，
        而 ``path_to`` 会返回长度为 1 的历史，症状是"模型看不到之前说过的话"，
        且**不报任何错**。
        """
        parent_id = self._head
        if parent_id is not None:
            return self.append_to(message, parent_id)
        return self._commit(message, parent_id=None)

    def append_to(self, message: AgentMessage, parent_id: str) -> str:
        """显式指定父节点追加。**这就是"分支"**——指向一个较早的节点即可。

        父节点不存在（``UnknownNode``）或已损坏（``TreeCorrupted``）时**拒绝**。
        拒绝而不是容忍，让"从坏节点分叉"在写入时就暴露，
        而不是等到读的时候才表现为"历史缺了一段"。
        """
        self._require_usable(parent_id)
        return self._commit(message, parent_id=parent_id)

    def _commit(self, message: AgentMessage, *, parent_id: str | None) -> str:
        """先落盘、再改内存（见类 docstring 的"内存不领先于磁盘"）。"""
        record = record_of(message, parent_id=parent_id)
        if self._store is not None:
            self._store.append([record])
        self._raw.append(record)
        self._rebuild()
        self._head = record.id
        return record.id

    def set_head(self, node_id: str) -> None:
        """把当前指针挪到 ``node_id``。**这就是"回滚"**（挪到祖先即可）。"""
        self._require_usable(node_id)
        self._head = node_id

    def _require_usable(self, node_id: str) -> None:
        if node_id not in self._by_id:
            raise UnknownNode(node_id, list(self._by_id))
        if node_id in self._corrupt:
            raise TreeCorrupted(
                f"节点 {node_id!r} 已损坏（{self._corrupt[node_id]}），不能用它作支点。"
                "从坏节点分叉会产出「历史缺一段」的分支，且读的时候才会暴露。"
            )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def path_to(self, node_id: str) -> list[str]:
        """返回**根到该节点**的节点 id 列表（含两端）。

        Q2 定的语义是"返回根到该节点的消息列表"，这里返回 **id** 而不是消息——
        因为分支与回滚需要的是 id（``append_to`` / ``set_head`` 都收 id），
        要消息用 :meth:`messages_to`。**把两种需求放在一个返回值里，
        调用方就总要拆一半丢掉。**

        只返回**本分支**的祖先，不含兄弟分支（门槛 G48）。
        """
        record = self._by_id.get(node_id)
        if record is None:
            raise UnknownNode(node_id, list(self._by_id))
        if node_id in self._corrupt:
            raise TreeCorrupted(
                f"节点 {node_id!r} 已损坏（{self._corrupt[node_id]}），无法回溯。"
            )

        ids: list[str] = []
        seen: set[str] = set()
        budget = len(self._by_id) + 1
        cursor: str | None = node_id
        steps = 0

        while cursor is not None:
            steps += 1
            # 防线二先行：任何情况下都不会挂死
            if steps > budget or cursor in seen:
                raise TreeCorrupted(
                    f"节点 {node_id!r} 的回溯链异常（{steps} 步未到根，"
                    f"当前 {cursor!r}）。健康节点不应出现这种情况——"
                    "说明索引与损坏报告不一致，是 gap 而不是数据问题。"
                )
            seen.add(cursor)
            ids.append(cursor)
            cursor = self._by_id[cursor].parent_id

        ids.reverse()
        return ids

    def messages_to(self, node_id: str) -> list[AgentMessage]:
        """该分支的完整历史消息（根 → 该节点）。"""
        return [message_of(self._by_id[nid]) for nid in self.path_to(node_id)]

    def history(self) -> list[AgentMessage]:
        """当前 head 分支的历史。**这是 ``context.py`` 会消费的入口。**"""
        if self._head is None:
            return []
        return self.messages_to(self._head)

    def record(self, node_id: str) -> NodeRecord:
        if node_id not in self._by_id:
            raise UnknownNode(node_id, list(self._by_id))
        return self._by_id[node_id]

    def children(self, node_id: str) -> list[str]:
        """直接子节点。**含损坏的孩子**——这是原始结构事实，
        调用方用 :meth:`corrupt_nodes` 判断哪些可用。"""
        return list(self._children.get(node_id, []))

    def roots(self) -> list[str]:
        """根节点（``parent_id is None``）的 id。"""
        return [record.id for record in self._raw if record.parent_id is None]

    def corrupt_nodes(self) -> dict[str, str]:
        """``{节点 id: 损坏原因}``。

        **它是"丢弃必须可见"在会话层的对应物**：加载时丢弃了哪些节点、
        为什么丢，必须能被拿到，而不是只发一条可能被过滤的 warning。
        """
        return dict(self._corrupt)

    @property
    def head_id(self) -> str | None:
        return self._head

    @property
    def clean(self) -> bool:
        """没有任何节点损坏。"""
        return not self._corrupt

    def __len__(self) -> int:
        """节点总数（**含损坏的**）——它反映磁盘上的事实。"""
        return len(self._by_id)
