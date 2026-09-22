"""函数式入口：把各层组装起来，跑一个任务。

这是"能启动"的最小闭环（架构方案 9 节 P1 的交付项 14）。

它**不含业务逻辑，只做接线**。理由很实际：CLI、测试、评测运行器三处
都需要同一套组装（provider + 注册表 + 上下文 + loop），各写一遍的结果
是三份会各自漂移的初始化代码——而漂移出来的差异，
症状是"评测跑通了但 CLI 跑不通"，排查方向完全错。

系统提示词为什么是常量、且刻意写短
    D4 / 架构 5.2 节：常驻区要 ≤ 3,500 token 且**逐字节稳定**。
    所以：

    - 不按任务动态调整（那会让常驻区每轮都变，prompt cache 全失效）；
    - 不放时间戳、会话 ID（同上）；
    - 写短不是省 token，是**把预算留给历史与工具输出**。

    参考数据（2026-09-21 实测常驻区，口径 = ``estimate_text`` 粗估）：

    ==================== ===========
    组成                  token
    ==================== ===========
    系统提示词（核心五工具）    131
    系统提示词（+两席联网）     286
    工具 schema（5 个核心）  1 018
    工具 schema（+两席）      1 729
    ``AGENTS.md`` 上限         800
    ==================== ===========

    即"两个联网工具全开 + 满额 AGENTS.md"约 **2 815**，在 D4 的 3 500 之内；
    余量留给 P4 的技能索引（≤600）。**实测数字与架构 5.1 表的估算有出入**
    （表里 schema 记 ~820，实测 1 018），已在 5.1 就地更正。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sigma import dotenv
from sigma_agent.agent_messages import AgentMessage, LlmMessageWrapper
from sigma_agent.checkpoint import ShadowCheckpoint
from sigma_agent.loop import AgentLoop
from sigma_agent.observe import LoopObserver
from sigma_agent.registry import ToolRegistry
from sigma_agent.types import TurnResult
from sigma_ai.base import NeverCancelled, SamplingParams
from sigma_ai.messages import UserMessage
from sigma_session.compact import CompactionOutcome, CompactionPolicy
from sigma_session.context import SessionContext
from sigma_session.tree import SessionTree
from sigma_session.resources import (
    AGENTS_MD_FILENAME,
    ProjectInstructions,
    load_project_instructions,
)
from sigma_tools.bash import BashTool
from sigma_tools.edit import EditTool
from sigma_tools.grep import GrepTool
from sigma_tools.read import ReadTool
from sigma_tools.web_fetch import WebFetchTool
from sigma_tools.web_search import WebSearchTool
from sigma_tools.write import WriteTool
from sigma_tools._firecrawl_quota import FirecrawlQuota
from sigma_tools._tavily_quota import TavilyQuota

if TYPE_CHECKING:
    from sigma_ai.base import BaseProvider


SYSTEM_PROMPT = """你是一个在本地工作区里干活的编程助手。

可用工具：
- read：读取文本文件，支持行范围（start_line / end_line）
- write：写入文件，覆盖原内容（新建文件或整体重写时用）
- edit：精确替换文件中的一段文本（修改已有文件时优先用它）
- bash：执行 bash 命令（列目录、建目录、运行测试等）
- grep：按正则搜索文件内容，返回 文件:行号:文本

工作方式：
1. 先看清楚再动手——不确定文件内容时先 read，不要凭猜测写。
2. 一次只做一件必要的事，不要把多步操作合成一次调用。
3. 完成后用一两句话说明你做了什么。

注意：
- 相对路径基于工作区根目录解析。
- write 不会自动创建父目录；建目录请用 bash 的 mkdir -p。
- bash 的命令没有任何过滤，执行前确认它符合当前任务。
"""


#: 联网工具的工具行。**只在启用时拼进系统提示词**——它进常驻区，
#: 所以"关掉时提示词逐字节不变"是有意义的性质（D4）。
#:
#: **工具行与注册表必须同源**（批次 7 的教训）：关掉 web_search 后若提示词仍写着它，
#: 模型会去调一个不存在的工具，然后拿到"未注册的工具"错误——那一轮的预算就白花了。
WEB_SEARCH_TOOL_LINE = (
    "- web_search：联网搜索（Tavily）。查库的最新用法、报错原因、版本变更等本地没有的信息。\n"
    "  返回标题 + URL + 摘要 + 可识别的发布时间。每次 1 credit（advanced 档 2），免费额度 1000/月。\n"
)
WEB_FETCH_TOOL_LINE = (
    "- web_fetch：精读指定 URL 的正文（Firecrawl）。**每次最多 2 条 URL**，每条 1 credit。\n"
    "  低质量来源与超过 2 年的旧结果会被代码层过滤，过滤条数会写在结果里。\n"
)

#: 调研纪律（批次 8）。它是 **Guide**（pi 笔记 13.2：前馈控制），
#: 与代码里那四道硬闸（Sensor）是一对——只用其中任何一个都会坏掉。
#:
#: 刻意拆成"通用"与"依赖精读"两段：第 3 条只在 web_fetch 启用时拼进去，
#: 否则提示词会指向一个不存在的工具（同上面那条纪律）。
RESEARCH_RULES: tuple[str, ...] = (
    "先说清要查什么再搜；具体查询优于宽泛查询，一次搜不到就换个说法，不要原地重试。",
    "只采信返回的结果条目本身；**不要**采用搜索服务生成的\"直接答案\"类内容。",
    "引用任何事实都要带 URL 与文档时间；新旧来源冲突时**采信更新的那条**。",
    "只找到旧资料时**直接说明**\"目前只有 X 年前的资料\"，不要把它当成现状。",
)
RESEARCH_FETCH_RULE = (
    "摘要够用就不要精读；只在\"关键结论依赖正文\"且\"摘要无法确认\"时才用 web_fetch。"
)


def build_system_prompt(*, web_search: bool = False, web_fetch: bool = False) -> str:
    """按启用的工具集生成系统提示词。

    为什么不是"提示词自己写死六个工具"：那样关掉某一个后提示词仍会告诉模型
    有一个并不存在的工具，模型会去调它，然后拿到 "未注册的工具" 错误。
    提示词与注册表必须同源。

    **只在会话开始时算一次**：它在常驻区里，会话内不能变（SessionContext 会算指纹并断言）。
    """
    lines: list[str] = []
    if web_search:
        lines.append(WEB_SEARCH_TOOL_LINE)
    if web_fetch:
        lines.append(WEB_FETCH_TOOL_LINE)
    if not lines:
        return SYSTEM_PROMPT

    rules = list(RESEARCH_RULES)
    if web_fetch:
        # 插在"只采信结果条目"与"引用要带时间"之间：先说能不能信，再说要不要深挖
        rules.insert(2, RESEARCH_FETCH_RULE)
    numbered = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, start=1))
    block = "".join(lines) + "\n调研纪律（联网时按这个顺序做）：\n" + numbered + "\n"

    marker = "\n工作方式："
    head, sep, tail = SYSTEM_PROMPT.partition(marker)
    return f"{head}\n{block}{sep}{tail}"


#: 联网搜索的额度账本落点。**在用户级配置目录**（仓库外）：
#: 配额是"这个 key 用了多少"，与具体工作区无关——放进工作区会让换个目录就重置计数，
#: 那正是"本地计数"最危险的一种失效方式。
DEFAULT_WEB_SEARCH_STATE = dotenv.USER_CONFIG_DIR / "tavily_usage.json"

#: 网页精读的额度账本落点。同上，且**必须与搜索分开**：
#: 两家是不同的服务、不同的额度池，合成一个文件就会 A 家花掉 B 家的额度。
DEFAULT_WEB_FETCH_STATE = dotenv.USER_CONFIG_DIR / "firecrawl_usage.json"


def web_search_tool(*, api_key: str, state_path: Path | None = None) -> WebSearchTool:
    """造一个联网搜索工具（连同它的额度账本）。

    单独一个工厂：测试要能直接拿到账本断言记账口径，不必先搭一个注册表。
    """
    quota = TavilyQuota(
        api_key=api_key, state_path=state_path or DEFAULT_WEB_SEARCH_STATE
    )
    return WebSearchTool(api_key=api_key, quota=quota)


def web_fetch_tool(*, api_key: str, state_path: Path | None = None) -> WebFetchTool:
    """造一个网页精读工具（连同它的额度账本）。理由同 :func:`web_search_tool`。"""
    quota = FirecrawlQuota(
        api_key=api_key, state_path=state_path or DEFAULT_WEB_FETCH_STATE
    )
    return WebFetchTool(api_key=api_key, quota=quota)


def default_registry(
    *,
    web_search: bool = False,
    tavily_api_key: str | None = None,
    web_fetch: bool = False,
    firecrawl_api_key: str | None = None,
) -> ToolRegistry:
    """内置工具集：read / write / edit / bash / grep 全部就位。

    bash 的风险要说清楚（详规 R1）：它以当前用户权限执行任意命令，
    P1 没有任何过滤与沙箱，防线只有 CLI 启动提示与评测的临时目录。

    **两个联网工具都默认关**（批次 7 详规 Q1，批次 8 沿用）：工具 schema 是常驻成本，
    没配 key 的机器不该为它们付那约 300 token。开不开由调用方决定
    （sigma.cli：解析到对应的 key 且没有 --no-web-search）——
    这样 default_registry() 的返回值与加这些能力之前**逐字节一致**，
    既有用例不受环境影响。

    两个开关**各自独立**：只配了 Tavily 的机器也能用搜索（只注册 web_search），
    反之亦然。合成一个开关的后果是"配了一个 key 却报另一个 key 缺失"。
    """
    registry = ToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(BashTool())
    registry.register(GrepTool())
    if web_search:
        if not tavily_api_key:
            raise ValueError(
                "web_search=True 但没有 tavily_api_key。密钥应由调用方解析后传入"
                "（sigma.dotenv.resolve_tavily_api_key），本层不自己去猜路径。"
            )
        registry.register(web_search_tool(api_key=tavily_api_key))
    if web_fetch:
        if not firecrawl_api_key:
            raise ValueError(
                "web_fetch=True 但没有 firecrawl_api_key。密钥应由调用方解析后传入"
                "（sigma.dotenv.resolve_firecrawl_api_key），本层不自己去猜路径。"
            )
        registry.register(web_fetch_tool(api_key=firecrawl_api_key))
    return registry


#: 压缩触发用的**上下文窗口**默认值。
#:
#: ⚠️ **这是一个保守下限，不是各家模型的真实窗口。** 真实窗口在
#: ``ProviderSpec`` 里**没法放**——那个类刻意只有三个字段（"只装怎么连上它"），
#: 而"模型能装多少"既不是连接参数、也会随模型换代而变。
#:
#: 取 32k 的理由是**两个方向的代价不对称**：
#:
#: - 猜**小**了 → 压缩提前触发，多花一次 LLM 调用。**贵，但不会坏。**
#: - 猜**大**了 → 该压不压，直到 provider 直接拒掉请求（``context_overflow``）。
#:   **坏，而且症状离根因很远**（表现出来像"模型突然不行了"）。
#:
#: 所以宁小勿大。调用方知道自己的模型时应当显式传 ``compaction_policy``
#: 覆盖它——这也是 ``CompactionPolicy.context_window_tokens`` 没有默认值的原因。
DEFAULT_CONTEXT_WINDOW_TOKENS = 32_000


def _real_clock() -> int:
    """当前时间戳。

    用 ``def`` 而不是 ``lambda``：**mypy strict 下 lambda 无法标注类型**，
    于是每次 ``clock()`` 调用都会报 ``no-untyped-call``。
    这是个容易忽略的约束——写 lambda 时不会想到它会被"调用类型检查"追上。
    """
    return int(time.time())


class InteractiveSession:
    """交互式会话：**跨轮复用同一个 ``SessionContext``**（门槛 G35）。

    为什么要有这个类，而不是让 CLI 自己拼
        ``-p`` 一次性模式与交互模式需要**同一套组装**（provider + 注册表 +
        上下文 + loop）。各写一遍的结果是两份会各自漂移的初始化代码，
        症状是"一次性模式跑通了但交互模式跑不通"——排查方向完全错。

    它**不做**什么
        不做中途打断 / 消息注入（P3 的 steering）。一条任务跑完整轮
        才能输入下一条，这是 P1 的明确边界，启动横幅里会写出来。
    """

    def __init__(
        self,
        *,
        provider: BaseProvider,
        workspace_root: Path,
        model: str,
        registry: ToolRegistry | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        max_rounds: int = 20,
        temperature: float = 0.0,
        observer: LoopObserver | None = None,
        emit: Callable[[str], None] | None = None,
        session_id: str = "sigma-session",
        project_instructions: str | None = None,
        compaction_policy: CompactionPolicy | None = None,
        tree: SessionTree | None = None,
        shadow_git_dir: Path | None = None,
        enable_checkpoint: bool = True,
    ) -> None:
        self._provider = provider
        self._model = model
        self._session_id = session_id
        self._registry = registry if registry is not None else default_registry()
        self._clock = _real_clock
        # 压缩策略：不传就用保守窗口的默认值（见 DEFAULT_CONTEXT_WINDOW_TOKENS）。
        # **默认开**而不是默认关：压缩是长会话能不能跑下去的前提，
        # 一个"默认关、要用得记得开"的能力等于没有——而它的症状是
        # 长会话跑到一半突然失败（R1）。
        self._compaction_policy = (
            compaction_policy
            if compaction_policy is not None
            else CompactionPolicy(context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS)
        )
        self._last_compaction: CompactionOutcome | None = None
        # 项目说明（``AGENTS.md``）：
        #   None → **自动**从 ``workspace_root/AGENTS.md`` 读（默认行为）
        #   ""   → 明确不要（调用方关掉它）
        #   其他 → 直接用调用方给的文本
        #
        # "None 与空串语义不同"是有意的：前者是"你替我找"，
        # 后者是"我不要"。把它们合成一个值，会让"想关掉"的人只能传一个
        # 恰好不存在的路径——那是靠约定维持的正确性，会漏。
        if project_instructions is None:
            self._instructions = load_project_instructions(
                workspace_root / AGENTS_MD_FILENAME
            )
        else:
            self._instructions = ProjectInstructions(text=project_instructions)

        # 影子 git checkpoint（D5 的 L2）。**位置由调用方给**（``shadow_git_dir``）：
        # 与 store.py / sessions.py 同一条判据——会话层与 agent 层不拼 ``Path.home()``，
        # 落点是产品壳的决定。不传就是"没有 checkpoint"（回放与测试路径的默认）。
        self._checkpoint: ShadowCheckpoint | None = None
        if enable_checkpoint and shadow_git_dir is not None:
            self._checkpoint = ShadowCheckpoint(
                root=shadow_git_dir, workspace=workspace_root
            )
            # 启动基线：**必须在任何写操作之前**。少了它，"第一个写批次前的快照"
            # 就是"已经被改过的状态"，第一次回滚无点可退（门槛 G65）。
            # 失败不阻断——它自己会降级（last_error 里留原因）。
            self._checkpoint.mark(label="baseline")
        # ``tree`` 由调用方传入（通常是 ``SessionTree.from_store(...)``）——
        # **会话接续的落点就在这里**：不传就是纯内存的新会话，
        # 传了就是接着那个会话往下走。本层不自己去读磁盘（谁决定策略谁传参）。
        self._context = SessionContext(
            system_prompt=system_prompt,
            tools_schema=self._registry.schemas(),
            clock=self._clock,
            session_id=session_id,
            project_instructions=self._instructions.text,
            tree=tree,
        )
        self._loop = AgentLoop(
            provider=provider,
            registry=self._registry,
            model=model,
            session_id=session_id,
            workspace_root=workspace_root,
            max_rounds=max_rounds,
            sampling=SamplingParams(temperature=temperature),
            signal=NeverCancelled(),
            clock=self._clock,
            emit=emit,
            observer=observer,
            checkpoint=self._checkpoint,
        )

    async def send(self, task: str) -> TurnResult:
        """发一条任务，跑完整轮，把产出追加回历史。

        追加这一步**必须在这里**——loop 参照 Pi 的形状不持有会话对象
        （详规 3.8.1），历史归调用方管。

        **压缩发生在追加新消息之前**：要压的是"已经攒下的历史"，
        把这一轮的新任务也算进去没意义（它才刚来，不可能在"最旧的一段"里）。
        """
        await self._compact_if_needed()
        now = self._clock()
        self._context.append(
            LlmMessageWrapper(
                timestamp=now, message=UserMessage(content=task, timestamp=now)
            )
        )
        result = await self._loop.run_turn(self._context.build_messages())
        self._context.append(*result.messages)
        return result

    async def _compact_if_needed(self) -> CompactionOutcome | None:
        """动态区超过策略阈值时压一次。压不了（没有可压的段）时返回 ``None``。

        **失败不打断会话**：压缩是"为了跑下去"的优化，压缩本身失败
        （provider 抖了一下 / 摘要为空）不该让用户丢掉整个会话——
        那就本末倒置了。所以异常在这里被降级成"这一轮不压"。
        """
        if self._compaction_policy is None:  # pragma: no cover - 目前恒为真
            return None
        if not self._context.should_compact(self._compaction_policy):
            return None
        try:
            outcome = await self._context.compact(
                policy=self._compaction_policy,
                provider=self._provider,
                model=self._model,
                signal=NeverCancelled(),
            )
        except Exception:
            # 刻意吞掉：见上面 docstring。**但不清空 `_last_compaction`**——
            # 上一次成功压缩的结果仍然是有效的，不该被一次失败抹掉。
            return None
        if outcome is not None:
            self._last_compaction = outcome
        return outcome

    @property
    def session_id(self) -> str:
        """本会话的 id。``--continue`` 的横幅要把它打出来——
        **用户得知道自己在续哪个会话**，否则"续上了吗"只能靠猜。"""
        return self._session_id

    @property
    def compaction_policy(self) -> CompactionPolicy | None:
        return self._compaction_policy

    @property
    def checkpoint(self) -> ShadowCheckpoint | None:
        """影子 checkpoint（``None`` = 本会话没有回滚保障）。

        CLI 用它打横幅与执行回滚——**可用性必须能被问出来**：
        静默降级的 checkpoint 等于没有 checkpoint。
        """
        return self._checkpoint

    @property
    def last_compaction(self) -> CompactionOutcome | None:
        """最近一次成功压缩的结果（CLI 用来告诉用户"压过了"）。"""
        return self._last_compaction

    def history(self) -> list[AgentMessage]:
        """**原始**历史消息（副本）。**跨轮累积**是这个类存在的意义（门槛 G35）。

        ⚠️ 这**不等于**发给模型的内容——压缩之后两者不同。
        要后者用 ``session.context.effective_history()``。
        分开的理由见 ``SessionContext.history``：前者是审计凭据，后者是本轮取舍。

        返回副本而不是内部列表：调用方拿去改不会污染会话状态。

        注意：历史经树往返（``message_to_dict`` → ``message_from_dict``），
        所以拿到的**不是**喂进去时的那些对象实例——值逐字段相等，
        同一性不保证（P2-3 起，见 ``SessionTree`` 的说明）。
        """
        return self._context.history()

    @property
    def project_instructions(self) -> ProjectInstructions:
        """已加载的项目说明（含是否被截断）。

        暴露它是为了让 CLI 能把"已加载 AGENTS.md（N token）"或
        "已截断，丢弃 N token"打给用户——**截断必须可见**，
        否则用户以为自己的约定全部生效了。
        """
        return self._instructions

    @property
    def context(self) -> SessionContext:
        """底层上下文。给需要读指纹 / 预算 / 会话树的地方用（CLI、评测、测试）。"""
        return self._context


async def run_task(
    task: str,
    *,
    provider: BaseProvider,
    workspace_root: Path,
    model: str,
    max_rounds: int = 20,
    registry: ToolRegistry | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    temperature: float = 0.0,
    emit: Callable[[str], None] | None = None,
    observer: LoopObserver | None = None,
    session_id: str = "sigma-session",
    project_instructions: str | None = None,
    compaction_policy: CompactionPolicy | None = None,
    tree: SessionTree | None = None,
    shadow_git_dir: Path | None = None,
    enable_checkpoint: bool = True,
) -> TurnResult:
    """跑一个任务，返回结果。**一次性会话**（发一条、跑完、结束）。

    ``workspace_root`` 决定两件事：工具里相对路径的基准（**它同时是 CLI 的
    ``--workspace``**），以及 L1 写路径约束的边界（写操作不得越出它）。
    它**不是沙箱**——bash 仍能以当前用户权限执行任意命令，兜底是 L2 的可回滚，
    不是拦截（见 D5 / architecture 6.3）。

    时间戳用真实时钟。若要让执行**确定**（回放测试要求两次逐字节一致），
    调用方应自行构造 ``AgentLoop`` 并注入固定 ``clock`` ——
    ``run_task`` 面向"真跑"，不为可复现性妥协。

    它复用 :class:`InteractiveSession` 的组装——**只有一处组装，不会漂移**。
    """
    session = InteractiveSession(
        provider=provider,
        workspace_root=workspace_root,
        model=model,
        registry=registry,
        system_prompt=system_prompt,
        max_rounds=max_rounds,
        temperature=temperature,
        observer=observer,
        emit=emit,
        session_id=session_id,
        project_instructions=project_instructions,
        compaction_policy=compaction_policy,
        tree=tree,
        shadow_git_dir=shadow_git_dir,
        enable_checkpoint=enable_checkpoint,
    )
    return await session.send(task)
