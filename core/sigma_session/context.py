"""会话上下文：常驻区（系统提示词 + 工具 schema + 项目说明）+ 树上的历史。

P1 是内存版线性历史；P2-3 起历史**改由 ``SessionTree`` 承载**。
为什么不留一个"列表模式"
    两种历史来源意味着两套 append / history 语义，而它们的差异
    （分支、损坏节点、落盘）只会在其中一条上被测试到。
    ``SessionTree()`` 不传 store 就是纯内存的——**一个实现已经覆盖两种用法**，
    再加一个列表模式纯属多一条会腐化的路径。

它服务于 ``sdk`` 与 ``cli``，**loop 本身不依赖它**——
loop 参照 Pi 的形状，接收消息列表、产出消息（详规 3.8.1）。
所以这里的职责只有三件：

1. 累积历史（委托给树）；
2. **强制常驻区纪律**——见下；
3. **强制常驻区预算**（P2-3 新增）——见下。

常驻区指纹为什么 P1 就要有
    架构方案 5.2 节的判断：把它做成断言的成本是零，收益是把一条容易违反的约定
    变成不可违反的约束。而它是"prompt cache 命中率"这个指标的前提。

    **P1 的启动路径上确实没有东西会改常驻区**，所以这个断言在 P1 不会真的触发。
    也正因如此，它必须配一个**能证明它会失败**的测试（门槛 G26 + 注入 E24）：
    **不会失败的断言会被后来者当成死代码删掉。**

    一个"永远为真"的断言不是保守，是负债——它占着位置却不产生信息。

常驻区预算断言（P2-3 新增）为什么同样必须配"会失败"的测试
    它比指纹断言**多一层风险**：AGENTS.md 的长度由**用户**决定。
    用户写一篇长文，常驻区就会超——而超限的症状是**变慢变贵**，
    静默、且离根因很远。

    但 P2-3 起 ``resources.py`` 会把 AGENTS.md 硬截断到 800 token，
    所以在这条路径上它也不会触发。**同样的处理：靠测试证明它会红**
    （门槛 G59 + 注入）。
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from sigma_agent.agent_messages import LlmMessageWrapper, convert_to_llm
from sigma_ai.messages import SystemMessage
from sigma_ai.tokens import estimate_messages, estimate_text

from sigma_session.compact import (
    CompactionOutcome,
    CompactionPolicy,
    compact_history,
    needs_compaction,
    split_for_compaction,
)
from sigma_session.tree import SessionTree, TreeCorrupted, UnknownNode

if TYPE_CHECKING:
    from collections.abc import Callable

    from sigma_agent.agent_messages import AgentMessage
    from sigma_ai.base import BaseProvider, CancelToken, SamplingParams

DEFAULT_RESIDENT_BUDGET_TOKENS = 3500
"""常驻区 token 上限（D4 / 架构 5.1 节）。

**这个数字不要为了让实现通过而放宽。** D4 是 prompt cache 的经济性主张——
放宽它等于把主张改掉以迁就实现。超了应当**砍常驻区里的东西**，
或者明确记下"这条主张现在不成立"。
"""


class ResidentRegionChanged(RuntimeError):
    """常驻区在会话内被改动。

    **这是一个必须在会话开始前就修掉的错误，不是一个可以恢复的状态。**
    常驻区变了，prompt cache 从变动点起全部失效——
    而失效是静默的（只是变慢变贵，不会报错），所以必须在这里崩掉。
    """


class ResidentBudgetExceeded(RuntimeError):
    """常驻区超过预算上限。

    与 :class:`ResidentRegionChanged` 分开，因为它们是**两个不同的时间点**：
    前者是"会话中途被改"（运行期违规），后者是"一开始就配得太大"（配置错误）。
    处置不同——前者要查是谁在动态改提示词，后者要减内容或砍上限。

    它进的是**常驻**区，所以每轮请求都要重付一次；
    超限不会报错，只会让每一次调用都更贵。
    """


class SessionContext:
    """会话上下文：常驻区（系统提示词 + 工具 schema + 项目说明）+ 树上的历史。

    常驻区在构造时冻结指纹，之后每次组装消息都校验一次
    （指纹 + 预算两道）。
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        tools_schema: list[dict[str, Any]],
        clock: Callable[[], str],
        session_id: str = "sigma-session",
        project_instructions: str = "",
        skill_index: str = "",
        tree: SessionTree | None = None,
        resident_budget_tokens: int = DEFAULT_RESIDENT_BUDGET_TOKENS,
    ) -> None:
        self._system_prompt = system_prompt
        self._tools_schema = tools_schema
        self._clock = clock
        self._session_id = session_id
        # 已经**截断过**的项目说明文本（resources.load_project_instructions 的产物）。
        # 本类不读文件——从哪读是产品壳的策略（见 resources.py 的分层说明）。
        self._project_instructions = project_instructions
        # 已经**渲染好**的技能索引（sigma_agent.skills.render_index 的产物）。
        #
        # 同样是"文本进、本类不读文件"：技能扫描要遍历目录、要处理坏文件，
        # 那些策略归产品壳。**这里只负责把它算进常驻区预算与指纹**——
        # 而这两件事必须在本层做，否则技能索引就成了预算管不到的盲区
        # （门槛 G59 会漏掉它），那和"偷偷往常驻区加东西"没区别。
        self._skill_index = skill_index
        self._resident_budget = resident_budget_tokens
        # 不传 store 的树就是纯内存的：一个实现覆盖两种用法。
        self._tree = tree if tree is not None else SessionTree()
        # 压缩视图（见 compact.py）：什么都没压时两者都是 None。
        self._summary: AgentMessage | None = None
        self._keep_from: str | None = None
        # 构造时冻结——之后每次组装都比对
        self._fingerprint = self._compute_fingerprint()

    # ------------------------------------------------------------------
    # 常驻区
    # ------------------------------------------------------------------

    def _resident_text(self) -> str:
        """常驻区里的文本部分 = 系统提示词 + 项目说明 + 技能索引。

        **顺序是有意的**：先"你是谁、有什么工具"，再"这个项目的额外约定"，
        最后才是"有哪些技能可以加载"。前两者是**人写的约束**，
        最后一项是**自动生成的能力目录**——把它们混在一起会让模型
        分不清哪句是硬规矩、哪句只是可选项。

        三段**空一段就少一段**，不做补位：全部为空时逐字节等于
        ``self._system_prompt``，于是"没有 AGENTS.md、没有技能的项目"
        与"加这些功能之前"的常驻区完全一致（与批次 7 的 ``default_registry()``、
        P2-3 的 ``project_instructions`` 是同一条纪律：
        新功能不该改变既有路径的字节）。
        """
        parts = [self._system_prompt]
        if self._project_instructions:
            parts.append(self._project_instructions)
        if self._skill_index:
            parts.append(self._skill_index)
        return "\n\n".join(parts)

    def _compute_fingerprint(self) -> str:
        """常驻区的指纹。

        ``sort_keys=True`` 是必要的：``tools_schema`` 是嵌套 dict，
        键序不稳定会让同一个内容的指纹每次不同，于是**断言天天误报**——
        而误报的断言会被关掉，比没有更糟。
        """
        payload = json.dumps(
            {"system": self._resident_text(), "tools": self._tools_schema},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        """当前常驻区指纹（测试与审计用）。"""
        return self._compute_fingerprint()

    def resident_text(self) -> str:
        """常驻区的**文本部分**（系统提示词 + 项目说明 + 技能索引）。

        公开它是因为"什么东西真的进了常驻区"必须可被**从外部断言**——
        P4-批次1 的 G76 就是靠它验"技能正文没进来"。
        若不公开，那条测试只能去读私有属性，而私有属性一改名测试就失效
        （或者更糟：改成静默读到 None 而断言仍然为真）。
        """
        return self._resident_text()

    def _tools_schema_text(self) -> str:
        """工具 schema 的规范化文本。

        单独抽出来是因为**它被算两次**（指纹与预算），
        两处各写一遍 ``json.dumps`` 参数的话，很容易只改一处。
        """
        return json.dumps(self._tools_schema, ensure_ascii=False, sort_keys=True)

    @property
    def resident_tokens(self) -> int:
        """常驻区的估算 token。

        ⚠️ **这是预警值，不是账本**（``sigma_ai/tokens.py`` 的定位）：
        真实用量以 provider 返回的 ``Usage`` 为准。
        它只用来在组装之前拦住明显超限的配置。
        """
        return estimate_text(self._resident_text()) + estimate_text(
            self._tools_schema_text()
        )

    @property
    def resident_budget_tokens(self) -> int:
        return self._resident_budget

    def verify_resident_region(self) -> None:
        """校验常驻区未变。不一致就抛——**不修、不忍、不warning**。"""
        current = self._compute_fingerprint()
        if current != self._fingerprint:
            raise ResidentRegionChanged(
                "常驻区在会话内被改动了。这是 prompt cache 的充要条件（D4 / 架构 5.2 节），"
                "所以它必须崩而不是警告——失效是静默的（只是变慢变贵，不会报错）。\n"
                f"  冻结时指纹 {self._fingerprint}\n"
                f"  当前指纹   {current}\n"
                "常见原因：把时间戳/随机 ID 写进了 system 消息，"
                "或按任务动态拼了系统提示词。"
            )

    def verify_resident_budget(self) -> None:
        """校验常驻区未超预算。超了就抛——理由见 :class:`ResidentBudgetExceeded`。

        **在组装消息之前调用**（与指纹校验同一个位置）：
        超限时先崩，而不是先发一次超大的请求再发现贵了。
        """
        used = self.resident_tokens
        if used > self._resident_budget:
            raise ResidentBudgetExceeded(
                f"常驻区 {used} token 超过上限 {self._resident_budget}"
                f"（D4 / 架构 5.1 节）。\n"
                f"  系统提示词 + 项目说明：{estimate_text(self._resident_text())} token\n"
                f"  工具 schema：{estimate_text(self._tools_schema_text())} token\n"
                "常驻区每轮都要重付一次，所以超限不会报错、只会一直更贵。\n"
                "处置：砍常驻区内容（如收紧 AGENTS.md 上限 / 少挂一个可选工具），"
                "**不要为了让实现通过而放宽预算**——那是把主张改掉以迁就实现。"
            )

    # ------------------------------------------------------------------
    # 历史（委托给树）
    # ------------------------------------------------------------------

    def append(self, *messages: AgentMessage) -> str | None:
        """追加消息，返回**最后一个节点 id**（没传消息时返回 ``None``）。

        **返回 id 是必要的，不是顺手**：P2-2 做了分支与回滚，
        而"从某个点分叉"需要那个点的 id。若这里返回 ``None``，
        调用方就只能绕过本层去用 ``context.tree``——
        于是"通过上下文追加"与"通过树追加"变成两条路径，
        迟早有人只在一处更新。**能力要么透传，要么别暴露。**

        多参数时返回最后一个的 id：语义是"我刚追加完这些，当前支的端点是它"。

        **只追加，不插入**（架构 5.2 节规则二）。插入会让 prompt cache
        从插入点起全部失效。如果确实需要把内容放到前面，正确做法是压缩
        （P2-4），不是 insert。
        """
        last: str | None = None
        for message in messages:
            last = self._tree.append(message)
        return last

    def history(self) -> list[AgentMessage]:
        """**原始**历史（树上的完整路径，根 → head）。

        ⚠️ **这不等于"发给模型的内容"** —— 压缩之后两者会不同。
        要后者请用 :meth:`effective_history`。

        把两者分开是有意的：原始历史是**审计凭据**（``store.py`` 明写
        "会话历史是事实记录"），而有效历史是**这一轮的性能取舍**。
        合成一个方法，就会有人拿"我看到的条数"去推断"模型看到的条数"。
        """
        return self._tree.history()

    # ------------------------------------------------------------------
    # 压缩（视图，不改历史）
    # ------------------------------------------------------------------

    def effective_history(self) -> list[AgentMessage]:
        """**发给模型**的历史：``[摘要?, *保留的最近 N 轮]``。

        压缩是一个**派生视图**，不写回树——见 ``compact.py`` 的模块 docstring
        （改写历史会毁掉审计链）。``keep_from`` 记的是**节点 id** 而不是下标：
        下标会在压缩之后继续追加消息时漂移，而 id 是稳定的。
        """
        full = self._tree.history()
        if self._summary is None or self._keep_from is None:
            return full

        try:
            path = self._tree.path_to(self._keep_from)
        except (TreeCorrupted, UnknownNode):
            # keep_from 那条消息已经不在树上了（会话文件被换过 / 损坏）。
            # **退回未压缩的历史**，而不是只发一条摘要：后者会让模型
            # 以为"之前什么都没发生"，丢的信息比"多发几条消息"多得多。
            return full

        # path_to(keep_from) 返回的是根到该节点的路径，取它的**长度**就知道
        # 要跳过前面多少条（历史与路径一一对应）。
        skip = len(path) - 1
        return [self._summary, *full[skip:]]

    @property
    def summary(self) -> AgentMessage | None:
        """当前生效的压缩摘要（没有则为 ``None``）。"""
        return self._summary

    def apply_compaction(self, summary: AgentMessage, *, keep_from: str) -> None:
        """把一次压缩的结果接到视图上。**不改树。**

        ``keep_from`` 是**保留窗口的第一条消息**的节点 id。
        由调用方在压缩前算好（它知道切分点），因为切分是按轮做的，
        而"轮"的知识在 ``compact.split_for_compaction`` 里。
        """
        self._summary = summary
        self._keep_from = keep_from

    def clear_compaction(self) -> None:
        """丢掉压缩视图，回到完整历史（诊断与测试用）。"""
        self._summary = None
        self._keep_from = None

    # ------------------------------------------------------------------
    # 预算
    # ------------------------------------------------------------------

    def dynamic_tokens(self) -> int:
        """**动态区**的估算 token（有效历史部分）。

        与 :attr:`resident_tokens` 分开算：两者的预算来源不同
        （常驻区是 D4 声明的，动态区是"窗口减去常驻区"），
        混在一起会让"该压不压"和"常驻区超了"两类问题分不清。
        """
        return estimate_messages(convert_to_llm(self.effective_history()))

    def should_compact(self, policy: CompactionPolicy) -> bool:
        """按策略判断是否需要压缩。**只判断，不执行**（执行要调模型，是异步的）。"""
        return needs_compaction(
            self.dynamic_tokens(),
            policy=policy,
            resident_tokens=self.resident_tokens,
        )

    async def compact(
        self,
        *,
        policy: CompactionPolicy,
        provider: BaseProvider,
        model: str,
        signal: CancelToken,
        sampling: SamplingParams | None = None,
    ) -> CompactionOutcome | None:
        """压缩一次并接到视图上。**没有可压的段时返回 ``None``。**

        切分与调用都在 ``compact`` 模块里（那里有完整的"为什么"），
        本方法只多做一件事：**把切分点翻译成节点 id**——因为
        ``history()`` 与 ``path_to()`` 一一对应，所以第一个被保留的
        消息的 id 就是 ``path[len(to_compact)]``。
        """
        messages = self.effective_history()
        outcome = await compact_history(
            messages,
            policy=policy,
            provider=provider,
            model=model,
            signal=signal,
            sampling=sampling,
            clock=self._clock,
        )
        if outcome is None:
            return None

        head = self._tree.head_id
        if head is None:  # pragma: no cover - 没有节点时不可能压出结果
            return None
        _, kept = split_for_compaction(
            messages, keep_recent_rounds=policy.keep_recent_rounds
        )
        path = self._tree.path_to(head)
        skip = len(messages) - len(kept)
        # **视图下标必须换算回原始路径下标**（2026-09-24 review 修复）：
        # 已压缩过的视图是 [摘要, *full[skip_old:]]——摘要是**不在树上的
        # 虚拟消息**，占视图下标 0；视图下标 i（i≥1）对应原始下标
        # skip_old + i - 1。直接拿视图下标索引原始路径，第二次压缩时
        # keep_from 会指向靠前得多的节点——第一次压掉的消息全部"复活"，
        # 压缩白做、历史暴涨，而且完全静默（仅当上次恰好只压 1 条时碰巧正确）。
        skip_old = 0
        offset = 0
        if self._summary is not None and self._keep_from is not None:
            try:
                skip_old = len(self._tree.path_to(self._keep_from)) - 1
                offset = 1
            except (TreeCorrupted, UnknownNode):
                # 旧 keep_from 已不在树上：effective_history 遇到这种情况会
                # 退回完整历史（无摘要视图），下标换算也按"无摘要"走。
                skip_old = 0
                offset = 0
        raw_index = max(0, skip_old + skip - offset)
        keep_from = path[raw_index] if raw_index < len(path) else head
        self.apply_compaction(outcome.summary, keep_from=keep_from)
        return outcome

    @property
    def tree(self) -> SessionTree:
        """底层的会话树。

        **暴露它，是为了让分支/回滚能被调用方使用**——否则 P2-2 做的能力
        在本层被重新封死，等于白做。
        """
        return self._tree

    def build_messages(self) -> list[AgentMessage]:
        """组装交给 loop 的完整消息列表。

        每次调用都**先校验常驻区**（指纹 + 预算）——这是这两条纪律唯一的执行点。
        顺序：系统消息在前，**有效历史**（可能被压缩）在后。

        ⚠️ 这里用 ``effective_history()`` 而不是 ``history()``：
        压缩的意义就在于"发给模型的那份更短"，用原始历史等于压缩白做。
        而摘要排在系统消息**之后**——它是动态内容，绝不能进常驻区（G51）。
        """
        self.verify_resident_region()
        self.verify_resident_budget()
        now = self._clock()
        system: AgentMessage = LlmMessageWrapper(
            timestamp=now,
            message=SystemMessage(content=self._resident_text(), timestamp=now),
        )
        return [system, *self.effective_history()]
