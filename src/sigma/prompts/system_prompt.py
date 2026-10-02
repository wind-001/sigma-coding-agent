"""系统提示词资产(P6 自 sdk.py 抽出):**全部提示词文案只住在这里**。

为什么独立成包(挑刺#3):系统提示词是 LLM Agent 的核心资产,此前埋在
sdk.py(1100 行)中间——装配代码改一行,滚过提示词的概率不为零。
D4 的"常驻区逐字节稳定"意味着这里每一个字都是**每轮请求都要重付的真金**。

同源纪律(批次 7 教训,本包存在的第二理由)
    工具行与注册表必须同源:关掉某工具后提示词若仍写着它,模型会去调
    一个不存在的工具。所有 *_TOOL_LINE 与 :func:`build_system_prompt`
    的开关一一对应,``InteractiveSession`` 不做"看参数猜提示词"的魔法。

参考数据(2026-09-21 实测,口径 = ``estimate_text`` 粗估):
    系统提示词(核心五工具) 131 / (+两席联网) 286。
    分项预算表见 ``config/resident_caps.py``(D4 v2,表即常量)。

分包约束:本包只 import providers(估算 token 用),不得 import
runtime/tools/sessions——提示词引用子 agent 上限数字的场景
(原 ``SUB_SYSTEM_PROMPT_PREFIX``)已随 ``MAX_RESULT_CHARS`` 迁
runtime/sub_agent.py(P6 拍板,层表方向)。
"""

from __future__ import annotations


#: todo 工具的工具行。todo 是**默认恒注册**的核心工具（见 :func:`default_registry`），
#: 所以它在默认提示词里——``SYSTEM_PROMPT = 基座 + 本行``（见下方构造）。
#: ``build_system_prompt(todo=False)`` 是评测 A/B 的"无 todo"消融档（P4-批次2 D-A2）：
#: 提示词与注册表必须同源（批次 7 教训），所以这一行必须能从提示词里拿掉。
TODO_TOOL_LINE = (
    "- todo：任务清单（.sigma/todo.json）。长任务先 create 拆解计划，每完成一步 update 状态；"
    "执行中发现后续划分或难度判断不对，用 revise 重排未开始的 pending 部分\n"
)

#: ask_user 工具行。恒注册(方向决策交还用户是产品决策),与注册表同源。
#: description 刻意写短——它进常驻区,每轮重付(load_skill 同一条纪律)。
ASK_USER_TOOL_LINE = (
    "- ask_user：进度走到需要**用户决定方向**的分叉时调用：给出问题、"
    "2–6 个候选选项与推荐项，等用户挑选后按选择继续。\n"
)

_SYSTEM_PROMPT_HEAD = """你是一个在本地工作区里干活的编程助手。

可用工具：
- read：读取文本文件，支持行范围（start_line / end_line）
- write：写入文件，覆盖原内容（新建文件或整体重写时用）
- edit：精确替换文件中的一段文本（修改已有文件时优先用它）
- bash：执行 bash 命令（列目录、建目录、运行测试等）
- grep：按正则搜索文件内容，返回 文件:行号:文本
"""

#: 「工作方式」第 3 条分有/无 todo 两版（P4-批次2 D-A2）：无 todo 消融档保留
#: "拆解"建议、去掉工具引用——不换文案的话，提示词会让模型去调一个不存在的
#: 工具（与工具行同源是同一条纪律，规则正文也不能豁免）。
#: 「计划随执行进化」（P4-批次4）：开工时的拆解基于当时的信息，执行到中途
#: 对剩下部分的理解更深——所以第 3 条要**明说可以改后续**。不说的话，
#: 模型会把第一版清单当圣旨，被自己开工时的无知锁死（工具里有 revise 但
#: 提示词不提 = 能力存在却无人使用，与"提示词指向不存在的工具"同样是不同源）。
_TODO_RULE_LINE = (
    "3. 长任务（3 步以上）先用 todo create 拆解成清单，按清单依次执行、逐步 update；"
    "**执行中若发现后续划分或难度判断不对，用 todo revise 重排未开始的部分**——"
    "最初那版只是基于当时信息的推测，不是定稿。\n"
)
_GENERIC_RULE_LINE = "3. 长任务（3 步以上）先拆解成小步骤，按步骤依次执行。\n"

_SYSTEM_PROMPT_RULES_HEAD = """工作方式：
1. 先看清楚再动手——不确定文件内容时先 read，不要凭猜测写。
2. 一次只做一件必要的事，不要把多步操作合成一次调用。
"""

_SYSTEM_PROMPT_RULES_TAIL = """4. 完成后用一两句话说明你做了什么。
5. 寒暄、问候、闲聊类消息（如"你好"）：直接简短友好回应即可——**不要**盘点项目、
   不要跑测试或门禁、不要调用任何工具；那些是开工任务才需要做的事。

注意：
- 相对路径基于工作区根目录解析。
- write 不会自动创建父目录；建目录请用 bash 的 mkdir -p。
- bash 的命令没有任何过滤，执行前确认它符合当前任务。
"""


def _rules_section(todo: bool) -> str:
    """「工作方式」整段。``todo=False`` 只换第 3 条，其余逐字节不变。"""
    return (
        _SYSTEM_PROMPT_RULES_HEAD
        + (_TODO_RULE_LINE if todo else _GENERIC_RULE_LINE)
        + _SYSTEM_PROMPT_RULES_TAIL
    )


SYSTEM_PROMPT = (
    _SYSTEM_PROMPT_HEAD + TODO_TOOL_LINE + ASK_USER_TOOL_LINE + "\n" + _rules_section(True)
)


#: 记忆纪律段的**稳定首行标记**：换表重建提示词时用它反推"构造期有没有
#: 记忆段"（见 InteractiveSession.set_web_tools）。为什么需要：system_prompt
#: 是调用方传进来的成品字符串，本类不知道它是用什么开关拼的——按开关猜会错
#: （裸常量 SYSTEM_PROMPT 恰等于 build_system_prompt(memory=False)，而
#: enable_memory 默认 True，猜错就是常驻区悄悄多出≈186 字节）。
#: 判定用整段的**首行**而不是全文：全文里含标点与措辞，改一次文案就失效。
MEMORY_SECTION_MARK = "记忆纪律（跨会话记忆已开启）："


def _memory_discipline_section() -> str:
    """跨会话记忆的"纪律段"（P5-批次3，memory=True 时追加，≈80 token）。

    为什么是**行为规程**而不是工具行：记忆没有专用工具（write/read 就是
    接口，详规 T7），所以这段教的是"什么值得记、写到哪、两条边界"。
    两条边界各有一层理由：
    - **不得与 AGENTS.md 矛盾**——AGENTS.md 是用户写的第一权威（详规 §1 边界表）；
    - **刚写的本会话索引不含**——索引在会话启动时冻结（D4：中途刷新=
      常驻区变化=缓存失效），这句必须告诉模型，否则它会以为"写了没生效"。
    """
    return (
        "\n\n" + MEMORY_SECTION_MARK + "\n"
        "- 值得写入 .sigma/memory/<slug>.md（首行 # 标题）：用户拍板、项目坑、"
        "失败尝试、环境事实；一次一篇，别写大杂烩。\n"
        "- 不值得写：一次性任务细节；与 AGENTS.md 矛盾的内容不写（它是第一权威）。\n"
        "- 刚写的记忆本会话的索引里没有（索引在会话开始时冻结），正文自己记住、"
        "直接用；下个会话自动出现在索引里。"
    )


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

#: 技能工具的**工具行**。同上面两条纪律：只在启用时进提示词，否则提示词会指向
#: 一个不存在的工具（模型会去调它，然后拿到"未注册的工具"错误）。
#:
#: 它与「可用技能」那段是**两件事**：这里说"有这么个工具"，
#: 那边说"有哪些技能可以取"。合成一段会让"没有技能时的提示词"
#: 与"没有这个工具时的提示词"分不清。
LOAD_SKILL_TOOL_LINE = (
    "- load_skill：按名加载某个技能的完整说明。**只在确实需要时用**——正文会占上下文。\n"
)

#: task 工具的工具行。同上面几条纪律：**只在 enable_sub_agent 时拼进提示词**——
#: 否则提示词会指向一个不存在的工具，模型去调它然后拿到"未注册"错误。
#: 它由调用方（CLI / 评测）经 ``build_system_prompt(task=True)`` 拼入，
#: InteractiveSession 不做"看参数猜提示词"的魔法。
TASK_TOOL_LINE = (
    "- task：派子任务给后台子 agent（独立上下文、同款工具）执行，不阻塞你；\n"
    "  status 查进度，完成后自动回报。探索/调研类工作适合派它；\n"
    "  子任务是后续步骤的前置依赖时，先做完其他事再收尾。\n"
)

RESEARCH_RULES: tuple[str, ...] = (
    "先说清要查什么再搜；具体查询优于宽泛查询，一次搜不到就换个说法，不要原地重试。",
    "只采信返回的结果条目本身；**不要**采用搜索服务生成的\"直接答案\"类内容。",
    "引用任何事实都要带 URL 与文档时间；新旧来源冲突时**采信更新的那条**。",
    "只找到旧资料时**直接说明**\"目前只有 X 年前的资料\"，不要把它当成现状。",
)
RESEARCH_FETCH_RULE = (
    "摘要够用就不要精读；只在\"关键结论依赖正文\"且\"摘要无法确认\"时才用 web_fetch。"
)


def build_system_prompt(
    *,
    web_search: bool = False,
    web_fetch: bool = False,
    skills: bool = False,
    task: bool = False,
    todo: bool = True,
    memory: bool = False,
) -> str:
    """按启用的工具集生成系统提示词。

    为什么不是"提示词自己写死所有工具"：那样关掉某一个后提示词仍会告诉模型
    有一个并不存在的工具，模型会去调它，然后拿到 "未注册的工具" 错误。
    提示词与注册表必须同源。

    **只在会话开始时算一次**：它在常驻区里，会话内不能变（SessionContext 会算指纹并断言）。

    ``skills`` 控制的是 **load_skill 这一行**，不是技能目录本身——
    目录是另一段（由 ``sigma.skills.scanner.render_index`` 渲染后注入常驻区）。
    两者分开是因为**没有技能时连工具都不该注册**（省 163 token 的 schema），
    而那时提示词里也就不该有这一行。

    ``task`` 同理：只在 ``enable_sub_agent`` 的主会话里为 True。

    ``todo`` 与其他开关方向相反：**默认开**（todo 恒注册是产品决策），
    ``todo=False`` 是评测 A/B 的"无 todo"消融档（P4-批次2 D-A2）——
    工具行与「工作方式」第 3 条一起换掉，注册表侧的同源开关是
    ``default_registry(todo=False)``。默认路径返回 ``SYSTEM_PROMPT`` 本身
    （逐字节一致由 test_system_prompt_is_byte_identical_when_disabled 钉住）。

    ``memory``（P5-批次3）：跨会话记忆的"纪律段"。注意它**不是工具行**——
    记忆没有专用工具（write/read 就是接口，详规 T7），这段是行为规程；
    索引本体由 ``SessionContext(memory_index=)`` 注入，两者必须同开同关
    （调用方用同一个旗标喂两处），否则"提示词说能记、常驻区没有索引"
    就是自相矛盾的常驻区。
    """
    head = _SYSTEM_PROMPT_HEAD + (TODO_TOOL_LINE if todo else "") + ASK_USER_TOOL_LINE
    rules_section = _rules_section(todo)
    memory_section = _memory_discipline_section() if memory else ""
    tool_lines: list[str] = []
    if web_search:
        tool_lines.append(WEB_SEARCH_TOOL_LINE)
    if web_fetch:
        tool_lines.append(WEB_FETCH_TOOL_LINE)
    if skills:
        tool_lines.append(LOAD_SKILL_TOOL_LINE)
    if task:
        tool_lines.append(TASK_TOOL_LINE)
    if not tool_lines:
        return head + "\n" + rules_section + memory_section

    block = "".join(tool_lines)
    # 调研纪律**只在联网时**加：它是给联网工具用的操作规程，
    # 与技能无关（早期版本的 `if not lines` 判据会把"只有技能"也算成联网）。
    if web_search or web_fetch:
        rules = list(RESEARCH_RULES)
        if web_fetch:
            # 插在"只采信结果条目"与"引用要带时间"之间：先说能不能信，再说要不要深挖
            rules.insert(2, RESEARCH_FETCH_RULE)
        numbered = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, start=1))
        block += "\n调研纪律（联网时按这个顺序做）：\n" + numbered + "\n"

    return f"{head}\n{block}\n{rules_section}{memory_section}"
