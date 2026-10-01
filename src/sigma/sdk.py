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

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sigma.config import settings
from sigma.prompts.system_prompt import (
    ASK_USER_TOOL_LINE,
    LOAD_SKILL_TOOL_LINE,
    RESEARCH_FETCH_RULE,
    RESEARCH_RULES,
    SYSTEM_PROMPT,
    TASK_TOOL_LINE,
    TODO_TOOL_LINE,
    WEB_FETCH_TOOL_LINE,
    WEB_SEARCH_TOOL_LINE,
    build_system_prompt,
)
from sigma.runtime.sub_agent import SUB_SYSTEM_PROMPT_PREFIX
from sigma.agent.messages import AgentMessage, LlmMessageWrapper
from sigma.security.shadow_checkpoint import ShadowCheckpoint
from sigma.hooks.base import (
    ApprovalHook,
    BaseHook,
    HookManager,
)
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.registry import ReloadReport, ToolRegistry
from sigma.skills.scanner import (
    SKILLS_DIRNAME,
    SkillMeta,
    SkillScan,
    discover_skills,
    render_index,
)
from sigma.agent.types import ToolContext, TurnResult
from sigma.providers import stamps
from sigma.providers.base import CancelToken, InterruptToken, NeverCancelled, SamplingParams
from sigma.providers.errors import ErrorCode, ProviderError
from sigma.providers.messages import UserMessage
from sigma.sessions.compaction import CompactionOutcome, CompactionPolicy
from sigma.sessions.context import SessionContext
from sigma.hooks.persist import SessionPersistHook
from sigma.memory.file_store import memory_dir_for, render_memory_index, scan_memory
from sigma.sessions.repo_map import build_repo_map
from sigma.observability.trace import TraceHook
from sigma.sessions.repair import repair_dangling_tool_results
from sigma.sessions.tree import SessionTree
from sigma.sessions.resources import (
    AGENTS_MD_FILENAME,
    ProjectInstructions,
    load_project_instructions,
)
from sigma.tools.builtin.ask_user import AskUserTool
from sigma.tools.builtin.bash import BashTool
from sigma.tools.builtin.edit import EditTool
from sigma.tools.builtin.grep import GrepTool
from sigma.tools.builtin.read import ReadTool
from sigma.tools.builtin.skill import LoadSkillTool
from sigma.runtime.sub_agent import (
    MAX_RESULT_CHARS,
    SubAgentFactory,
    SubAgentRounds,
    TaskTool,
)
from sigma.team.mailbox import Mailbox
from sigma.team.store import BoardStore
from sigma.tools.builtin.multi_agent import MultiAgentTool
from sigma.tools.builtin.team import TeamBoard
from sigma.tools.builtin.todo import TodoTool
from sigma.tools.builtin.web_fetch import WebFetchTool
from sigma.tools.builtin.web_search import WebSearchTool
from sigma.tools.builtin.write import WriteTool
from sigma.tools.quota.firecrawl import FirecrawlQuota
from sigma.tools.quota.tavily import TavilyQuota

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from sigma.providers.base import BaseProvider


#: 联网搜索的额度账本落点。**在用户级配置目录**（仓库外）：
#: 配额是"这个 key 用了多少"，与具体工作区无关——放进工作区会让换个目录就重置计数，
#: 那正是"本地计数"最危险的一种失效方式。
DEFAULT_WEB_SEARCH_STATE = settings.USER_CONFIG_DIR / "tavily_usage.json"

#: 网页精读的额度账本落点。同上，且**必须与搜索分开**：
#: 两家是不同的服务、不同的额度池，合成一个文件就会 A 家花掉 B 家的额度。
DEFAULT_WEB_FETCH_STATE = settings.USER_CONFIG_DIR / "firecrawl_usage.json"


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
    skills: Sequence[SkillMeta] = (),
    todo: bool = True,
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
    # ask_user 恒注册（P3-批次2，星辰）：方向决策交还用户。
    # schema 进常驻区；description 刻意写短，SDK/测试不需要时可自行剔除。
    registry.register(AskUserTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(BashTool())
    registry.register(GrepTool())
    # todo **默认注册**（P4 任务清单）：它的文件是工具自己的账本（.sigma/todo.json），
    # 不依赖任何外部服务或配置，"没有条件"的情况不存在——所以产品路径上没有开关。
    # SYSTEM_PROMPT 里的 todo 工具行与本行必须同源（批次 7 教训）。
    # 唯一的例外是评测：``todo=False`` 是 A/B 的"无 todo"消融档（P4-批次2 D-A2），
    # 提示词侧的同源开关是 ``build_system_prompt(todo=False)``——两个开关必须一起用。
    if todo:
        registry.register(TodoTool())
    if web_search:
        if not tavily_api_key:
            raise ValueError(
                "web_search=True 但没有 tavily_api_key。密钥应由调用方解析后传入"
                "（sigma.config.settings.resolve_tavily_api_key），本层不自己去猜路径。"
            )
        registry.register(web_search_tool(api_key=tavily_api_key))
    if web_fetch:
        if not firecrawl_api_key:
            raise ValueError(
                "web_fetch=True 但没有 firecrawl_api_key。密钥应由调用方解析后传入"
                "（sigma.config.settings.resolve_firecrawl_api_key），本层不自己去猜路径。"
            )
        registry.register(web_fetch_tool(api_key=firecrawl_api_key))
    # load_skill **只在真有技能时注册**：它的 schema 实测约 163 token，
    # 一个没有技能目录的项目不该为它付这份常驻成本——
    # 与两个联网工具"有 key 才注册"是同一条判据。
    if skills:
        registry.register(LoadSkillTool(skills=skills))
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


def _skills_label(root: Path, workspace: Path) -> str:
    """技能根目录的**人可读位置**，给索引头部用（模型据此知道文件在哪）。

    能相对工作区表示就相对表示（``extensions/skills/``）——绝对路径在索引里
    既长又是本机路径，对模型没有价值。相对不了（比如技能目录在工作区之外）
    才退回绝对路径：**报一个准确的路径，好过报一个好看但错的**。
    """
    try:
        return f"{root.relative_to(workspace).as_posix()}/"
    except ValueError:
        return f"{root.as_posix()}/"


def scan_skills(
    workspace_root: Path, *, skills_root: Path | None = None
) -> tuple[SkillScan, str]:
    """扫技能 → ``(扫描结果, 索引文本)``。

    产品壳需要**在构造会话之前**拿到这两样（打横幅、拼提示词），所以它得能单独调。

    **本函数会被调两次**（产品壳一次、``InteractiveSession`` 内部一次）。
    这在纸面上是 TOCTOU：两次之间理论上可能不一致。
    但代价只是"横幅上少一个刚加进来的技能"，而收益是
    **不必在 sdk 与 shell 之间传一份状态**——那份状态才是真会腐化的东西
    （谁负责更新它、什么时候失效，都没有好的答案）。
    **启动时多读两个目录，换掉一个跨层状态**，这笔账划算。
    """
    root = (
        skills_root
        if skills_root is not None
        else workspace_root / "extensions" / SKILLS_DIRNAME
    )
    scan = discover_skills(root)
    return scan, render_index(scan.skills, root_label=_skills_label(root, workspace_root))


def _real_clock() -> str:
    """当前时间戳。

    用 ``def`` 而不是 ``lambda``：**mypy strict 下 lambda 无法标注类型**，
    于是每次 ``clock()`` 调用都会报 ``no-untyped-call``。
    这是个容易忽略的约束——写 lambda 时不会想到它会被"调用类型检查"追上。
    """
    return stamps.now()


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
        extra_hooks: Sequence[BaseHook] = (),
        emit: Callable[[str], None] | None = None,
        session_id: str = "sigma-session",
        project_instructions: str | None = None,
        compaction_policy: CompactionPolicy | None = None,
        enable_compaction: bool = True,
        tree: SessionTree | None = None,
        shadow_git_dir: Path | None = None,
        enable_checkpoint: bool = True,
        skills_root: Path | None = None,
        todo_steer_interval: int = 10,
        enable_todo: bool = True,
        enable_sub_agent: bool = False,
        enable_team_tasks: bool = False,
        sub_agent_max_concurrent: int = 3,
        sub_agent_rounds: SubAgentRounds | None = None,
        signal: CancelToken | None = None,
        tool_lock: asyncio.Lock | None = None,
        approval: ApprovalHook | None = None,
        ask: Callable[[str, list[str], int | None], Awaitable[str]] | None = None,
        repair_dangling: bool = True,
        checkpoint_watermark_bytes: int | None = None,
        enable_trace: bool = True,
        enable_memory: bool = True,
        enable_repo_map: bool = True,
        enable_extensions: bool = True,
    ) -> None:
        """``sub_agent_rounds``：子 agent 的轮数预算**三档**（low/medium/high）。

        原来是一个固定值 50——没有凭据，且单次子任务的成本上限被抬到
        ~450k token。改成三档后由**派发的模型**按难度选（默认 medium=20，
        与主任务默认轮数一致）。详见 :class:`sigma.runtime.sub_agent.SubAgentRounds`。
        """
        self._provider = provider
        self._model = model
        self._session_id = session_id
        self._workspace_root = workspace_root
        # 子 agent 工厂要重建同款组装（见 _make_sub_agent_factory），
        # 这几样先存起来——它们本来只为构造 AgentLoop 存在，现在多一个读者。
        self._emit = emit
        # 渲染等"跨会话共享"的钩子由调用方以列表注入（P4-批次6）：
        # 每个会话自建 HookManager（持久化钩子绑定本会话上下文），
        # 共享钩子逐个注册进每一条会话的总线——子 agent 的渲染可见性同源。
        self._extra_hooks = tuple(extra_hooks)
        # L3 审批钩子(决策型,P3-批次2):None = 不注册 = 默认放行
        # (评测/子 agent 不接审批,行为与没有 L3 之前一致)。
        self._approval = approval
        # ask_user 工具的交互通道:None = 非交互,工具自动采用推荐项。
        self._ask = ask
        # 中途打断与双队列(P3-批次2 下半场,星辰):signal 未显式传入时,
        # 每次 send 新建 InterruptToken(打断状态不跨任务泄漏);
        # 显式传入(评测的 NeverCancelled)则 interrupt() 无操作。
        self._user_signal = signal
        self._turn_signal: InterruptToken | None = None
        self._steering: list[AgentMessage] = []
        self._followups: list[str] = []
        self._turn_running = False
        self._shadow_git_dir = shadow_git_dir
        self._registry = (
            registry if registry is not None else default_registry(todo=enable_todo)
        )
        if not enable_todo:
            if "todo" in self._registry.names():
                raise ValueError(
                    "registry 里已注册 todo 工具，与 enable_todo=False 冲突。"
                    "无 todo 消融档请用 default_registry(todo=False) 或自行剔除，"
                    "提示词侧用 build_system_prompt(todo=False) 保持同源。"
                )
            todo_steer_interval = 0
        self._clock = _real_clock
        # 压缩策略：不传就用保守窗口的默认值（见 DEFAULT_CONTEXT_WINDOW_TOKENS）。
        # **默认开**而不是默认关：压缩是长会话能不能跑下去的前提，
        # 一个"默认关、要用得记得开"的能力等于没有——而它的症状是
        # 长会话跑到一半突然失败（R1）。
        #
        # ``enable_compaction=False`` 是**显式关断**（EvalProfile 的 B1 档），
        # 它的优先级必须压过上面的"默认开"：``None`` 在下面会被替换成
        # 默认策略，所以"关压缩"不能靠传 ``None`` 表达——那是 §11.5 第 1 条
        # 踩出来的坑。三条分支的次序就是这个优先级，不要重排。
        if not enable_compaction:
            self._compaction_policy = None
        elif compaction_policy is not None:
            self._compaction_policy = compaction_policy
        else:
            self._compaction_policy = CompactionPolicy(
                context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS
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
        # P4-批次7：库里所有会话共享对象，本会话的快照走自己的分支（session_id 即分支名）。
        self._checkpoint: ShadowCheckpoint | None = None
        if enable_checkpoint and shadow_git_dir is not None:
            self._checkpoint = ShadowCheckpoint(
                root=shadow_git_dir,
                workspace=workspace_root,
                branch=self._session_id,
                watermark_bytes=checkpoint_watermark_bytes,
            )
        # 构造期**不打** baseline（G65 已修订为懒基线）：loop 的
        # ``_mark_before_writes`` 在写批次**执行前**打快照，首个写批次快照
        # 天然就是"任何写之前的干净状态"——启动那记是纯冗余，而它把
        # 整个工作区扫描压在了启动路径上（家目录实测单次扫描 65 秒）。
        # 技能：**自动从 ``<workspace>/extensions/skills`` 扫**（与 AGENTS.md 同一处置），
        # 但扫描结果要**同时**喂给两处，少一处就是半截功能：
        #   - 常驻区：渲染成"可用技能"那段文本（模型据此知道有哪些技能）
        #   - 注册表：``load_skill`` 工具（模型据此去取正文）
        # 只喂常驻区 → 模型看得到、调不动；只喂注册表 → 它不知道有哪些名字可调。
        self._skills_root = (
            skills_root
            if skills_root is not None
            else workspace_root / "extensions" / SKILLS_DIRNAME
        )
        self._skill_scan, self._skill_index = scan_skills(
            workspace_root, skills_root=skills_root
        )
        # 跨会话记忆（P5-批次3，D4 v2 预算修订后默认开）：扫 .sigma/memory/
        # 一次、渲染索引进常驻区。**快照语义**（G884）：会话内不重扫——
        # 模型刚写的记忆本会话不可见，下个会话自动出现。
        # 空目录 → 空索引 → 常驻区与无记忆机制逐字节一致（G881）。
        # --no-memory 时整段跳过，连目录探测都不做。
        self._enable_memory = enable_memory
        self._memory_scan = (
            scan_memory(memory_dir_for(workspace_root)) if enable_memory else None
        )
        self._memory_index = (
            render_memory_index(self._memory_scan)
            if self._memory_scan is not None
            else ""
        )
        # repo map(P1 收尾,吃 resident_caps 具名预留 500):工作区结构快照,
        # 会话启动扫一次进常驻区——**会话内冻结**(D4),文件变化下个会话可见。
        # 空工作区 → 空地图 → 常驻区与无此功能逐字节一致。
        # --no-repo-map 时整段跳过,连目录遍历都不做(与 --no-memory 同款)。
        self._repo_map = build_repo_map(workspace_root) if enable_repo_map else ""
        # 有技能就必须有 load_skill。**这里会往调用方传进来的注册表里补一个工具**——
        # 看似越权，但反过来（有技能却没这个工具）的后果是"模型看得到技能却调不动"，
        # 在运行期表现成"它就是不用技能"，排查方向完全错。
        # 判据：**宁可在启动时补一个明确的注册项，也不要留一个只能在运行期看出来的断点。**
        if self._skill_scan.skills and LoadSkillTool.name not in self._registry.names():
            self._registry.register(LoadSkillTool(skills=self._skill_scan.skills))
        # task 工具与信箱接线（P4 sub_agent，星辰拍板 2026-09-23）：
        # 主会话创建锁与 TaskTool；AgentLoop 只拿两个回调与锁——
        # loop 不 import 工具层（兄弟层契约），与 todo steering 同一条纪律。
        # 锁**主子共用**：主 loop 的写批次与后台子 agent 的写批次互斥（读写冲突
        # 的防护），checkpoint mark 一并被罩住。dispatch/status 是 read_only，
        # 在锁外的 readonly 组执行——派发永远不会被在跑的写批次卡死。
        self._tool_lock: asyncio.Lock | None = tool_lock
        self._mailbox_drain: Callable[[], list[AgentMessage]] | None = None
        self._mailbox_wait: Callable[[], Awaitable[list[AgentMessage]]] | None = None
        # 团队协作域的共享资源（①-c）：主会话与子会话**必须共享同一实例**
        # （同一块板 + 同一把 store 锁，claim 互斥才成立）。这里先定型为 None：
        # 子会话工厂的闭包在 dispatch 时才执行，那时团队段可能还没跑到——
        # 不先置 None，`enable_team_tasks=False` 时工厂读它就是个 AttributeError。
        self._team_store: BoardStore | None = None
        self._team_mailbox: Mailbox | None = None
        if enable_sub_agent:
            if "task" in self._registry.names():
                raise ValueError(
                    "registry 里已注册 task 工具，与 enable_sub_agent=True 冲突。"
                    "task 只能由本类注册（否则工厂与注册表可能不一致）。"
                )
            # 子会话由工厂传入主会话的锁（tool_lock 参数）；主会话自己没有时新建。
            if self._tool_lock is None:
                self._tool_lock = asyncio.Lock()
            rounds = (
                sub_agent_rounds if sub_agent_rounds is not None else SubAgentRounds()
            )
            task_tool = TaskTool(
                factory=self._make_sub_agent_factory(self._tool_lock, rounds),
                max_concurrent=sub_agent_max_concurrent,
                rounds=rounds,
                team_hint=enable_team_tasks,
            )
            self._registry.register(task_tool)
            self._mailbox_drain = task_tool.drain_completed
            self._mailbox_wait = task_tool.wait_and_drain
        # team_board 与信箱接线（P4 团队任务，详规 §2.3）：主会话注册（创建/分配
        # 任务），子 agent 经 registry clone **共享同一个实例**——共享同一块板、
        # 同一把 store 锁（claim 互斥的前提）；信箱按 ctx.session_id 分文件。
        # "lead" 别名解析到本会话 id。**默认 False**：不传时注册表与提示词
        # 与加它之前逐字节一致；CLI 侧默认随 --sub-agent，可 --no-team 单独关。
        if enable_team_tasks:
            if "team_board" in self._registry.names():
                raise ValueError(
                    "registry 里已注册 team_board 工具，与 enable_team_tasks=True 冲突。"
                    "team_board 只能由本类注册（lead 身份必须与本会话 id 一致）。"
                )
            self._team_store = BoardStore()
            self._team_mailbox = Mailbox()
            self._registry.register(
                TeamBoard(
                    lead_session_id=session_id,
                    role="lead",
                    store=self._team_store,
                    mailbox=self._team_mailbox,
                )
            )
            self._registry.register(
                MultiAgentTool(
                    lead_session_id=session_id,
                    workspace_root=self._workspace_root,
                    factory=self._make_sub_agent_factory(
                    self._tool_lock or asyncio.Lock(), rounds
                ),
                    store=self._team_store,
                    mailbox=self._team_mailbox,
                    max_rounds=rounds.medium,
                )
            )
        # ``tree`` 由调用方传入（通常是 ``SessionTree.from_store(...)``）——
        # **会话接续的落点就在这里**：不传就是纯内存的新会话，
        # 传了就是接着那个会话往下走。本层不自己去读磁盘（谁决定策略谁传参）。
        #
        # 扩展工具装载（D3 / 详规 P3-扩展热重载 §4）：<workspace>/extensions/*.py，
        # 模块级 ``TOOLS: list[BaseTool]`` 即注册项，source=文件路径。
        # 失败文件打报告继续（与技能发现"问题必须可见"同判据），报告经
        # :attr:`extension_reports` 暴露给横幅与测试。排在 task/team 注册
        # **之后**：扩展与 harness 关键工具重名时，得到的是一条失败报告
        # 而不是构造期崩溃——坏扩展不该拖死会话。CLI 在打横幅前会装载过
        # 一次（可见性），这里再装一次：同 source 是**幂等替换**，不重名。
        self._extension_reports: list[ReloadReport] = (
            list(self._registry.load_extensions(workspace_root / "extensions"))
            if enable_extensions
            else []
        )
        # 断点续跑的前置修复（P4-批次5 Q3 拍板）：接续的树若停在
        # "assistant 带 tool_calls 但结果缺失"（中断所致），先补齐合成结果。
        # 无悬空时零写入零开销（repair 对健康会话是纯读扫描）。
        if repair_dangling and tree is not None:
            repair_dangling_tool_results(tree, clock=self._clock)
        self._context = SessionContext(
            system_prompt=system_prompt,
            tools_schema=self._registry.schemas(),
            clock=self._clock,
            session_id=session_id,
            project_instructions=self._instructions.text,
            skill_index=self._skill_index,
            memory_index=self._memory_index,
            repo_map=self._repo_map,
            tree=tree,
        )
        # 钩子总线（P4-批次5，星辰拍板）：持久化是**订阅事件的钩子**，
        # 不再是 send() 末尾的一次批量追加——每一次 LLM 返回、每一个工具
        # 结果落定、每一次注入都立即写穿会话树，中断即停在最后一条
        # 已发生的消息上。调用方传入自己的 manager 时也必须挂上持久化钩子
        # （它是正确性要求，不是可选能力）；钩子的其余消费者同理自行注册。
        self._hooks = HookManager()
        # 持久化钩子持引用（热重载 rebind 用，G-HR-3）：reload_tools 重建
        # context 后必须把它接到新 context 上——接线必须可被断言。
        self._persist_hook = SessionPersistHook(self._context)
        self._hooks.register(self._persist_hook)
        # 观测层（P5-批次1，Q2 拍板默认开）：trace 与会话文件同目录、
        # 同名不同后缀，落点由"有没有 store"决定——纯内存树没有落点，
        # 评测与子 agent 的静默是同款承诺（零钩子 = 行为不变），不是遗漏。
        if enable_trace and tree is not None and tree.store is not None:
            self._hooks.register(TraceHook(session_id, tree.store.path.parent))
        for hook in self._extra_hooks:
            self._hooks.register(hook)
        if self._approval is not None:
            self._hooks.register_approval(self._approval)
        self._loop = AgentLoop(
            provider=provider,
            registry=self._registry,
            model=model,
            session_id=session_id,
            workspace_root=workspace_root,
            max_rounds=max_rounds,
            sampling=SamplingParams(temperature=temperature),
            signal=signal if signal is not None else NeverCancelled(),
            clock=self._clock,
            emit=emit,
            checkpoint=self._checkpoint,
            todo_steer_interval=todo_steer_interval,  # enable_todo=False 时已置 0
            tool_lock=self._tool_lock,
            mailbox_drain=self._mailbox_drain,
            mailbox_wait=self._mailbox_wait,
            hooks=self._hooks,
            ask=self._ask,
            steering_drain=self._drain_steering,
        )

    def _make_sub_agent_factory(
        self, tool_lock: asyncio.Lock, rounds: SubAgentRounds
    ) -> SubAgentFactory:
        """造子 agent 工厂（闭包捕获主会话的组装知识，每次 dispatch 调用一次）。

        子会话的 registry = 主 registry 的**克隆**，两处刻意改动：

        1. **去掉 task**——递归禁止的构造上排除（星辰原话"工具中不允许包含
           task 工具"）。克隆意味着 web/技能/其余工具**与主会话天然同款**，
           不需要把开关参数抄一份（抄一份必然漂移）。
        2. **todo 换独立账本**——共享会让"至多一条 running"被静默破坏。
        """
        async def factory(
            description: str, ctx: ToolContext, sub_session_id: str,
            max_rounds: int,
        ) -> TurnResult:
            # 子会话的 registry = 主 registry 的克隆（同款工具、独立登记簿），
            # 构造上排除 task（递归禁止）与 todo（子会话换独立账本，见下）。
            # ①-c（2026-09-30 拍板）再加两个排除项：
            #   team_board —— 换成 **worker 面**后重注册（见下），越权 op 在子会话的
            #     工具面上根本不存在（详规 §2 的"物理堵门"原本只写在文档里）；
            #   multi_agent —— 详规 §5 明写不做"多团队并存（单主会话一板）"，
            #     子会话能自己 start 引擎会让这条边界失守。
            # 必须先 exclude 再 register：registry 重名抛错且**拒绝静默覆盖**（G24）。
            # 用 ``ToolRegistry.clone`` 而不是手写循环：同一个概念不该有两份实现。
            sub_registry = self._registry.clone(
                exclude=("task", "todo", "team_board", "multi_agent")
            )
            # worker 面**共享主会话的 store/mailbox**：另造一份就是两把锁，
            # claim 互斥（G-TEAM-2）会变成名义上的——与 multi_agent 引擎同一条纪律。
            # store 为 None = 本会话没开团队模式（--no-team），那就什么都不注册。
            if self._team_store is not None and self._team_mailbox is not None:
                sub_registry.register(
                    TeamBoard(
                        lead_session_id=self._session_id,
                        role="worker",
                        store=self._team_store,
                        mailbox=self._team_mailbox,
                    )
                )
            # 子 agent 的 todo 换独立账本（主清单不被子触碰，"至多一条 running"
            # 各自成立）；**但只在主会话有 todo 时才注册**——无 todo 消融档
            # （enable_todo=False，P4-批次2 D-A2）不能经子会话把 todo 偷渡回来。
            if "todo" in self._registry.names():
                sub_registry.register(
                    TodoTool(relative_path=f".sigma/todo-{sub_session_id}.json")
                )
            sub_names = sub_registry.names()
            sub_session = InteractiveSession(
                provider=self._provider,
                workspace_root=self._workspace_root,
                model=self._model,
                registry=sub_registry,
                # 提示词与克隆后的 registry **同源**（批次 7 教训）：
                # 按 registry 里实际有哪些工具拼条件行，角色行放最前。
                system_prompt=SUB_SYSTEM_PROMPT_PREFIX
                + build_system_prompt(
                    web_search="web_search" in sub_names,
                    web_fetch="web_fetch" in sub_names,
                    skills="load_skill" in sub_names,
                    todo="todo" in sub_names,
                ),
                # 轮数预算由派发方按难度档位给（low/medium/high）——
                # 子会话不自定预算（见 SubAgentRounds 的取值依据）。
                max_rounds=max_rounds,
                extra_hooks=self._extra_hooks,
                approval=self._approval,
                ask=self._ask,
                emit=self._emit,
                session_id=sub_session_id,
                # AGENTS.md 用主会话已加载的文本（同一份，不重读文件）
                project_instructions=self._instructions.text,
                compaction_policy=self._compaction_policy,
                enable_compaction=self._compaction_policy is not None,
                enable_checkpoint=self._checkpoint is not None,
                shadow_git_dir=self._shadow_git_dir,
                skills_root=self._skills_root,
                tool_lock=tool_lock,
                # 子 agent 不透传观测（P5-批次1 R5）：子任务的开销经信箱
                # 注入的部分可见于主 trace；完整覆盖属 task 深度观测，另立批次。
                enable_trace=False,
                # 子 agent 不透传记忆（P5-批次3）：子任务短生命周期，
                # 索引属主会话；子任务需要上下文由派发方在 description 里给。
                enable_memory=False,
                # repo map 同款:子会话不付地图钱(主会话已带,切片自会引用)。
                enable_repo_map=False,
                # 扩展不重复装载:子 registry 是主 registry 的克隆,扩展工具
                # 已经在里面(source 登记也随定义复制);详规 §3"evals/子 agent
                # 不受影响"指的就是子会话不做自己的装载,更没有 /reload。
                enable_extensions=False,
                # 取消传播：主会话被取消时子任务同步停（signal 从派发时的
                # ToolContext 里来——那是主 loop 的取消令牌）。
                signal=ctx.signal,
            )
            return await sub_session.send(description)

        return factory

    def interrupt(self) -> bool:
        """打断当前正在跑的任务(可从任意协程/线程调用)。

        返回是否真的打断了一枚在跑的令牌。协作式:在跑的工具先完成,
        流在下一个块边界停;树上状态已持久化,断点重续免费。
        """
        token = self._turn_signal
        if token is None:
            return False
        token.cancel()
        return True

    @property
    def turn_running(self) -> bool:
        """是否有任务正在跑(读线程据此把输入路由为 steering/排队/打断)。"""
        return self._turn_running

    def submit_steering(self, text: str) -> None:
        """任务运行中注入补充指导(下一轮模型调用前生效,REPL 线程喂入)。"""
        now = self._clock()
        self._steering.append(
            LlmMessageWrapper(
                timestamp=now, message=UserMessage(content=text, timestamp=now)
            )
        )

    def _drain_steering(self) -> list[AgentMessage]:
        if not self._steering:
            return []
        drained = self._steering[:]
        self._steering.clear()
        return drained

    def submit_followup(self, text: str) -> None:
        """排队下一条任务:当前任务完成后由 REPL 自动执行。"""
        self._followups.append(text)

    def has_followups(self) -> bool:
        return bool(self._followups)

    def pop_followup(self) -> str:
        return self._followups.pop(0)

    def reload_tools(self, source: str | None = None) -> list[ReloadReport]:
        """热重载扩展工具并重建常驻区（详规 §2/§3；SDK 公开方法，评测 B3 臂用）。

        ``source=None`` 重载**全部**已装载来源；给来源串只重载那一个
        （未知来源 ``KeyError``）。没有任何已装载来源时返回 ``[]`` 且
        **不重建**——常驻区不可能变，重建是纯冗余。

        重建是 D4 的**显式违约点**：用户敲 /reload 就是"我知道缓存要失效"。
        树、历史与压缩视图经 :meth:`SessionContext.rebuild_with_tools` 原样
        移交；persist 钩子 rebind 到新 context；trace 钩子不动（路径来自
        store，换路径会丢会话连续性）。全部报告失败时 schema 未变，
        重建等价于原样再冻结（幂等，无副作用）。
        """
        sources = [source] if source is not None else self._registry.extension_sources()
        if not sources:
            return []
        reports = [self._registry.reload_source(s) for s in sources]
        self._context = self._context.rebuild_with_tools(self._registry.schemas())
        self._persist_hook.rebind(self._context)
        return reports

    async def send(self, task: str) -> TurnResult:
        """发一条任务，跑完整轮。

        追加历史**不在这里**——loop 参照 Pi 的形状不持有会话对象
        （详规 3.8.1），而产出消息的持久化由 :class:`SessionPersistHook`
        在每个时机（LLM 返回 / 工具结果落定 / 注入）经钩子事件即时完成
        （P4-批次5）：中断时树上停在最后一条已发生的消息，续跑从那里开始。

        **压缩发生在发任务之前**：要压的是"已经攒下的历史"，
        把这一轮的新任务也算进去没意义（它才刚来，不可能在"最旧的一段"里）。
        """
        await self._compact_if_needed()
        now = self._clock()
        self._context.append(
            LlmMessageWrapper(
                timestamp=now, message=UserMessage(content=task, timestamp=now)
            )
        )
        # 每次任务一枚新令牌:打断只对当前任务生效,不跨任务泄漏
        #(外部固定信号=评测路径,interrupt() 无操作)。
        self._turn_signal = InterruptToken() if self._user_signal is None else None
        self._turn_running = True
        try:
            return await self._attempt_turn()
        finally:
            self._turn_running = False

    async def _attempt_turn(self, *, may_recover: bool = True) -> TurnResult:
        """跑一轮，带 context_overflow 恢复（Review-2026-09-26 P0）。

        架构 4.1：``context_overflow`` 是压缩的**第二条触发路径**
        （第一条是 send 边界的本地估算，见 ``_compact_if_needed``）。
        恢复序列 = **修复悬空 → 强制压缩一次视图 → 重跑整轮**：

        - 为什么重跑而不是轮中续跑：loop 是无状态的，消息视图是
          run_turn 启动时的快照，压缩之后必须重建——轮中改它违反
          loop 的形状（详规 3.8.1）。增量持久化让重跑不重复付费：
          已落盘的消息就在历史里。
        - 为什么先修悬空：错误轮的 partial assistant 可能带未应答的
          tool_calls（流在批次执行前断掉），不修就重发等于送一个协议错上去。
        - **只恢复一次**：再次溢出说明压缩救不了（历史太短 / 已压过 /
          单条消息巨大），如实返回 error——不无限循环、不静默截断。

        ``provider.stream`` 抛 ``ProviderError`` 的路径做防御性捕获：
        当前 OpenAI 兼容 provider 把错误走 ErrorEvent 数据路径，但抛异常
        是 ``BaseProvider`` 允许的形态，换一个实现不该让恢复失效。
        """
        # 轮前悬空修复（不变量：InteractiveSession 的树在轮边界协议干净）
        repair_dangling_tool_results(self._context.tree, clock=self._clock)
        try:
            result = await self._loop.run_turn(
                self._context.build_messages(), signal=self._turn_signal
            )
        except ProviderError as exc:
            if not (may_recover and exc.code is ErrorCode.CONTEXT_OVERFLOW):
                raise
            if not await self._compact_for_overflow():
                raise
            return await self._attempt_turn(may_recover=False)
        if (
            may_recover
            and result.status == "error"
            and result.error_code == ErrorCode.CONTEXT_OVERFLOW.value
        ):
            if not await self._compact_for_overflow():
                return result
            return await self._attempt_turn(may_recover=False)
        return result

    async def _compact_for_overflow(self) -> bool:
        """溢出后**强制**压缩一次视图。压不了（关压缩/无可压段/压缩失败）返回 False。

        与 ``_compact_if_needed`` 的分工：那条是"按阈值主动压"，
        这条是"provider 已经拒了，能压多少压多少"。压不出结果
        （``compact`` 返回 None = 没有可压的段）就如实说救不了。
        失败不放大成新的失败源——与 ``_compact_if_needed`` 同判据。
        """
        if self._compaction_policy is None:
            return False
        try:
            outcome = await self._context.compact(
                policy=self._compaction_policy,
                provider=self._provider,
                model=self._model,
                signal=NeverCancelled(),
            )
        except Exception:
            return False
        if outcome is not None:
            # 与 _compact_if_needed 同一可见性通道:CLI 用 last_compaction
            # 告诉用户"压过了"。溢出恢复的压缩不该是隐形的。
            self._last_compaction = outcome
        return outcome is not None

    async def _compact_if_needed(self) -> CompactionOutcome | None:
        """动态区超过策略阈值时压一次。压不了（没有可压的段）时返回 ``None``。

        **失败不打断会话**：压缩是"为了跑下去"的优化，压缩本身失败
        （provider 抖了一下 / 摘要为空）不该让用户丢掉整个会话——
        那就本末倒置了。所以异常在这里被降级成"这一轮不压"。
        """
        if self._compaction_policy is None:
            # 显式关断（B1 档）走这里。以前标着 pragma: no cover——
            # 当时 None 不可达；enable_compaction 落地后它是真实分支，
            # 有测试钉着（test_interactive_session_explicit_compaction_off）。
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
    def skill_scan(self) -> SkillScan:
        """本次启动扫到的技能（含问题清单）。CLI 用它打横幅。"""
        return self._skill_scan

    @property
    def extension_reports(self) -> list[ReloadReport]:
        """启动时装载扩展的报告（成功与失败都在）。横幅与 G-HR-4 的断言面——
        坏扩展静默消失的症状是"我加了工具它怎么不用"。"""
        return list(self._extension_reports)

    @property
    def persist_hook(self) -> SessionPersistHook:
        """持久化钩子实例。热重载的 rebind 接线必须可被从外部断言（G-HR-3），
        与 :attr:`checkpoint`"可用性必须能被问出来"同一条判据。"""
        return self._persist_hook

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
    extra_hooks: Sequence[BaseHook] = (),
    approval: ApprovalHook | None = None,
    ask: Callable[[str, list[str], int | None], Awaitable[str]] | None = None,
    session_id: str = "sigma-session",
    project_instructions: str | None = None,
    compaction_policy: CompactionPolicy | None = None,
    enable_compaction: bool = True,
    tree: SessionTree | None = None,
    shadow_git_dir: Path | None = None,
    enable_checkpoint: bool = True,
    skills_root: Path | None = None,
    todo_steer_interval: int = 10,
    enable_todo: bool = True,
    enable_sub_agent: bool = False,
    enable_team_tasks: bool = False,
    sub_agent_max_concurrent: int = 3,
    sub_agent_rounds: SubAgentRounds | None = None,
    checkpoint_watermark_bytes: int | None = None,
    enable_trace: bool = True,
    enable_memory: bool = True,
    enable_repo_map: bool = True,
    enable_extensions: bool = True,
) -> TurnResult:
    """跑一个任务，返回结果。**一次性会话**（发一条、跑完、结束）。

    ``workspace_root`` 决定两件事：工具里相对路径的基准（**它同时是 CLI 的
    ``--workspace``**），以及 L1 写路径约束的边界（写操作不得越出它）。
    它**不是沙箱**——bash 仍能以当前用户权限执行任意命令，兜底是 L2 的可回滚，
    不是拦截（见 D5 / architecture 6.3）。

    ``enable_compaction=False`` 是评测用的**显式消融开关**（B1 档，见
    :mod:`sigma.eval_profile`）：关掉自动压缩。它与 ``enable_checkpoint``
    同构——评测要能声明"无 harness 干预"档，否则 B1 对照无从成立。

    ``enable_todo=False`` 同理（P4-批次2 D-A2）：todo 工具 A/B 的"无 todo"
    对照臂。调用方须同步传 ``system_prompt=build_system_prompt(todo=False)``
    保持提示词与注册表同源（评测运行器 task_runner.py 就是这么做的）。

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
        extra_hooks=extra_hooks,
        approval=approval,
        ask=ask,
        emit=emit,
        session_id=session_id,
        project_instructions=project_instructions,
        compaction_policy=compaction_policy,
        enable_compaction=enable_compaction,
        tree=tree,
        shadow_git_dir=shadow_git_dir,
        enable_checkpoint=enable_checkpoint,
        skills_root=skills_root,
        todo_steer_interval=todo_steer_interval,
        enable_todo=enable_todo,
        enable_sub_agent=enable_sub_agent,
        enable_team_tasks=enable_team_tasks,
        sub_agent_max_concurrent=sub_agent_max_concurrent,
        sub_agent_rounds=sub_agent_rounds,
        checkpoint_watermark_bytes=checkpoint_watermark_bytes,
        enable_trace=enable_trace,
        enable_memory=enable_memory,
        enable_repo_map=enable_repo_map,
        enable_extensions=enable_extensions,
    )
    return await session.send(task)
