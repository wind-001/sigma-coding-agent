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

from sigma_agent.agent_messages import LlmMessageWrapper
from sigma_ai.messages import SystemMessage
from sigma_ai.tokens import estimate_text

from sigma_session.tree import SessionTree

if TYPE_CHECKING:
    from collections.abc import Callable

    from sigma_agent.agent_messages import AgentMessage

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
        clock: Callable[[], int],
        session_id: str = "sigma-session",
        project_instructions: str = "",
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
        self._resident_budget = resident_budget_tokens
        # 不传 store 的树就是纯内存的：一个实现覆盖两种用法。
        self._tree = tree if tree is not None else SessionTree()
        # 构造时冻结——之后每次组装都比对
        self._fingerprint = self._compute_fingerprint()

    # ------------------------------------------------------------------
    # 常驻区
    # ------------------------------------------------------------------

    def _resident_text(self) -> str:
        """常驻区里的文本部分 = 系统提示词 + 项目说明。

        两者拼接而不是发成两条 system 消息：**指纹关心的是整块内容**，
        消息条数只是外壳。合成一块后，"改了 AGENTS.md"与"改了提示词"
        走的是同一条校验路径，不会有一条被漏掉。

        项目说明为空时**逐字节等于** ``self._system_prompt``——
        这样"没有 AGENTS.md 的机器"与"加这个功能之前"的提示词完全一致
        （与批次 7 的 ``default_registry()`` 是同一条纪律：
        新功能不该改变既有路径的字节）。
        """
        if not self._project_instructions:
            return self._system_prompt
        return f"{self._system_prompt}\n\n{self._project_instructions}"

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
        """当前分支的历史（根 → head）。"""
        return self._tree.history()

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
        顺序：系统消息在前，历史在后。
        """
        self.verify_resident_region()
        self.verify_resident_budget()
        now = self._clock()
        system: AgentMessage = LlmMessageWrapper(
            timestamp=now,
            message=SystemMessage(content=self._resident_text(), timestamp=now),
        )
        return [system, *self._tree.history()]
