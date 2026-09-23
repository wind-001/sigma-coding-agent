"""技能系统（``sigma_agent.skills`` + ``sigma_tools.skill``）的门禁测试。

对应 ``docs/plans/P4-批次1-Skill系统-详规.md`` §8 的门槛 **G75–G78**。

**这个文件里最要紧的是两组断言**

1. **索引进常驻区且稳定**（G75）。顺序一乱，常驻区就每轮变一次，
   而症状是 prompt cache 静默失效——只是变慢变贵，不会报错。
2. **正文绝不进常驻区**（G76）。这是整个渐进披露设计的地基：
   正文进去了，每加载一个技能就把之前的缓存全部作废。

两组都是"**看不见的失败**"——没有断言就永远不会有人发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sigma_agent.skills import (
    DEFAULT_MAX_BODY_TOKENS,
    SkillMeta,
    discover_skills,
    load_body,
    parse_frontmatter,
    render_index,
    strip_frontmatter,
)
from sigma_ai.tokens import estimate_text
from sigma_session.context import SessionContext
from sigma_tools.skill import LoadSkillTool

CLOCK = lambda: "2026-01-01T00:00:00.000"  # noqa: E731 - 固定时钟

SKILL_TEMPLATE = """---
name: {name}
description: {description}
---

# {name} 正文

这是 **{name}** 的正文，里面有一句只在正文出现的话：{body_only}。
"""


def _make_skill(
    root: Path,
    dirname: str,
    *,
    name: str | None = None,
    description: str = "一个技能",
    body_only: str = "正文独有短语",
    filename: str | None = None,
) -> Path:
    directory = root / dirname
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (filename or f"{dirname}.md")
    path.write_text(
        SKILL_TEMPLATE.format(
            name=name if name is not None else dirname,
            description=description,
            body_only=body_only,
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# frontmatter 解析
# ---------------------------------------------------------------------------


def test_parse_frontmatter_basic() -> None:
    meta = parse_frontmatter("---\nname: a\ndescription: 说明\n---\n正文")

    assert meta == {"name": "a", "description": "说明"}


def test_no_frontmatter_returns_none() -> None:
    """**没有** frontmatter 返回 ``None``，与"有但为空"（空 dict）分开。

    两者处置不同：前者是"忘了写"，后者是"写空了"，报错文案不一样，
    而调用方需要区分。
    """
    assert parse_frontmatter("就是一段正文") is None
    assert parse_frontmatter("---\nname: a\n") is None  # 结束围栏没出现


def test_frontmatter_ignores_unparsable_lines() -> None:
    """不认的行**跳过**而不是报错——将来想加注释或别的语法时，老代码不会炸。

    真正必需字段的缺失由 ``discover_skills`` 报，那才是该报错的地方。
    """
    meta = parse_frontmatter("---\n# 注释\nname: a\n这不是键值对\ndescription: d\n---\n")

    assert meta == {"name": "a", "description": "d"}


def test_strip_frontmatter_keeps_body_only() -> None:
    body = strip_frontmatter("---\nname: a\n---\n\n# 正文\n内容")

    assert "name" not in body
    assert "正文" in body


def test_strip_frontmatter_without_fence_is_identity() -> None:
    assert strip_frontmatter("没有 frontmatter") == "没有 frontmatter"


# ---------------------------------------------------------------------------
# 发现（G75 的顺序稳定性、G78 的坏输入必须被报告）
# ---------------------------------------------------------------------------


def test_discover_missing_root_is_not_an_error(tmp_path: Path) -> None:
    """技能目录不存在 → 空结果。**不是错误**：还没建技能的项目不该为此报错。"""
    scan = discover_skills(tmp_path / "没有这个目录")

    assert scan.skills == []
    assert scan.problems == []


def test_discover_finds_skills_and_sorts_by_name(tmp_path: Path) -> None:
    """**G75 的条件之一：顺序必须稳定。**

    故意用"创建顺序与字典序相反"（zeta 先建）来证明输出**是按名字排的**，
    而不是碰巧跟了文件系统返回的顺序。
    """
    _make_skill(tmp_path, "zeta")
    _make_skill(tmp_path, "alpha")

    scan = discover_skills(tmp_path)

    assert [s.name for s in scan.skills] == ["alpha", "zeta"]
    assert scan.problems == []


def test_discover_is_stable_across_calls(tmp_path: Path) -> None:
    """调两次结果完全一样（渲染出来的索引也逐字节相同）。"""
    for name in ("b", "a", "c"):
        _make_skill(tmp_path, name)

    first = render_index(discover_skills(tmp_path).skills, root_label="skills/")
    second = render_index(discover_skills(tmp_path).skills, root_label="skills/")

    assert first == second


def test_discover_location_is_relative_and_informative(tmp_path: Path) -> None:
    """``location`` 是**相对技能根目录**的路径。

    它必须真的有信息量——如果把文件名写死成 ``<name>.md``，
    这个字段就只是 ``<name>/<name>.md`` 的复述，一个字的信息都没多。
    """
    _make_skill(tmp_path, "alpha", filename="SKILL.md")

    scan = discover_skills(tmp_path)

    assert scan.skills[0].location == "alpha/SKILL.md"


def test_discover_reports_directory_without_markdown(tmp_path: Path) -> None:
    """**G78**：空目录要被**报告**，不能静默跳过。

    静默跳过的症状是"我加了技能它怎么不用"——与手工清单漂移是同一种失败。
    """
    (tmp_path / "empty").mkdir()

    scan = discover_skills(tmp_path)

    assert scan.skills == []
    assert len(scan.problems) == 1
    assert "empty" in scan.problems[0]


def test_discover_reports_multiple_markdown(tmp_path: Path) -> None:
    """一个目录里多个 md → 歧义，报告并跳过（不猜）。"""
    _make_skill(tmp_path, "dup")
    (tmp_path / "dup" / "another.md").write_text("x", encoding="utf-8")

    scan = discover_skills(tmp_path)

    assert scan.skills == []
    assert "多个" in scan.problems[0]


def test_discover_reports_missing_description(tmp_path: Path) -> None:
    """缺 ``description`` 的技能不可用——索引里那一条就没法写。

    宁可不收录（并报告），也不要收一条"名字后面跟个空白"的索引。
    """
    directory = tmp_path / "nodesc"
    directory.mkdir()
    (directory / "nodesc.md").write_text("---\nname: nodesc\n---\n正文", encoding="utf-8")

    scan = discover_skills(tmp_path)

    assert scan.skills == []
    assert "description" in scan.problems[0]


def test_discover_reports_duplicate_names(tmp_path: Path) -> None:
    """两个目录自称同一个技能名 → 后者被跳过并报告。

    ``load_skill`` 是按名取的，重名意味着"取到的可能是另一个"。
    """
    _make_skill(tmp_path, "one", name="same")
    _make_skill(tmp_path, "two", name="same")

    scan = discover_skills(tmp_path)

    assert len(scan.skills) == 1
    assert "重复" in scan.problems[0]


def test_discover_falls_back_to_directory_name(tmp_path: Path) -> None:
    """frontmatter 没写 ``name`` 时用目录名——**目录名就是那个技能**。"""
    directory = tmp_path / "fromdir"
    directory.mkdir()
    (directory / "fromdir.md").write_text(
        "---\ndescription: 只有说明\n---\n正文", encoding="utf-8"
    )

    scan = discover_skills(tmp_path)

    assert scan.skills[0].name == "fromdir"


# ---------------------------------------------------------------------------
# 索引渲染
# ---------------------------------------------------------------------------


def test_render_index_empty_is_empty_string() -> None:
    """没有技能 → **空串**，不是一段空的"可用技能："。

    那几十个 token 没技能时不该花；而一个空小节还会让模型
    以为"技能系统坏了"。
    """
    assert render_index([], root_label="skills/") == ""


def test_render_index_contains_three_fields() -> None:
    """索引里必须同时有 name / description / location —— 需求的三件事。"""
    skills = [
        SkillMeta(name="a", description="做 A", location="a/a.md", source=Path("a/a.md"))
    ]

    text = render_index(skills, root_label="extensions/skills/")

    assert "a：" in text
    assert "做 A" in text
    assert "a/a.md" in text
    assert "extensions/skills/" in text  # 根目录只在头部出现一次


def test_render_index_root_label_appears_once() -> None:
    """根目录**只在头部说一次**：每条都带全路径会把预算吃掉约 3 倍。"""
    skills = [
        SkillMeta(name=n, description="d", location=f"{n}/{n}.md", source=Path("x"))
        for n in ("a", "b", "c")
    ]

    text = render_index(skills, root_label="extensions/skills/")

    assert text.count("extensions/skills/") == 1


# ---------------------------------------------------------------------------
# 正文加载
# ---------------------------------------------------------------------------


def test_load_body_strips_frontmatter(tmp_path: Path) -> None:
    """正文里**不该再出现元数据**——它已经进索引了，重复是白花 token。"""
    path = _make_skill(tmp_path, "a", description="说明文字")
    meta = discover_skills(tmp_path).skills[0]

    result = load_body(meta)

    assert "说明文字" not in result.text
    assert "正文" in result.text
    assert not result.truncated
    assert path.exists()


def test_load_body_truncates_with_visible_marker(tmp_path: Path) -> None:
    """**丢弃必须可见**：截断后正文里要有标记，说清丢了多少、怎么拿全文。"""
    directory = tmp_path / "big"
    directory.mkdir()
    (directory / "big.md").write_text(
        "---\nname: big\ndescription: 大技能\n---\n" + "很长的正文句子。\n" * 800,
        encoding="utf-8",
    )
    meta = discover_skills(tmp_path).skills[0]

    result = load_body(meta, max_tokens=200)

    assert result.truncated
    assert result.tokens <= 200, f"截断后 {result.tokens} 超上限"
    assert "已截断" in result.text
    assert "read" in result.text  # 行动指引


def test_load_body_unreadable_returns_empty(tmp_path: Path) -> None:
    """文件读不出来 → 空文本 + 不抛。由工具层转成模型可见的错误。"""
    path = _make_skill(tmp_path, "gone")
    meta = discover_skills(tmp_path).skills[0]
    path.unlink()

    result = load_body(meta)

    assert result.text == ""
    assert result.tokens == 0


def test_default_body_budget_is_documented_value() -> None:
    assert DEFAULT_MAX_BODY_TOKENS == 1500


# ---------------------------------------------------------------------------
# load_skill 工具（G77）
# ---------------------------------------------------------------------------


def _tool(tmp_path: Path) -> LoadSkillTool:
    return LoadSkillTool(skills=discover_skills(tmp_path).skills)


async def test_tool_returns_body_for_known_name(tmp_path: Path) -> None:
    from sigma_agent.types import ToolContext
    from sigma_ai.base import NeverCancelled

    _make_skill(tmp_path, "alpha", body_only="正文独有")
    tool = _tool(tmp_path)
    ctx = ToolContext(
        session_id="s", workspace_root=tmp_path, signal=NeverCancelled()
    )

    result = await tool.run(tool.params(name="alpha"), ctx)

    assert not result.is_error
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "正文独有" in text
    assert result.details["name"] == "alpha"
    assert result.details["truncated"] is False


async def test_tool_unknown_name_lists_available(tmp_path: Path) -> None:
    """**G77 本体**：认不出名字时要**把可用的名字列出来**。

    模型手里只有常驻区那份目录，可能记错名字（大小写、单复数）。
    只回一句"没有这个技能"，它只能瞎猜着重试——**而每次重试都是一次完整的模型调用**。
    """
    from sigma_agent.types import ToolContext
    from sigma_ai.base import NeverCancelled

    _make_skill(tmp_path, "alpha")
    _make_skill(tmp_path, "beta")
    tool = _tool(tmp_path)
    ctx = ToolContext(
        session_id="s", workspace_root=tmp_path, signal=NeverCancelled()
    )

    result = await tool.run(tool.params(name="不存在的"), ctx)

    assert result.is_error
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "alpha" in text and "beta" in text
    assert result.details["available"] == ["alpha", "beta"]


async def test_tool_with_no_skills_says_so(tmp_path: Path) -> None:
    """一个技能都没有时，错误文案要说"当前没有任何技能"，不是列一个空列表。"""
    from sigma_agent.types import ToolContext
    from sigma_ai.base import NeverCancelled

    tool = _tool(tmp_path)
    ctx = ToolContext(
        session_id="s", workspace_root=tmp_path, signal=NeverCancelled()
    )

    result = await tool.run(tool.params(name="x"), ctx)

    assert result.is_error
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "没有任何技能" in text


def test_tool_is_read_only() -> None:
    """只读 → 它能进 loop 的并发批次（``_execute_batch`` 按这个字段分流）。"""
    assert LoadSkillTool.read_only is True
    assert LoadSkillTool.name == "load_skill"


def test_tool_description_stays_short() -> None:
    """工具行**进常驻区**，每轮都要重付一次。给个上限防止它慢慢变长。

    这条不是洁癖：它和另外 7 个工具的 schema 共享 3500 的总预算。
    """
    assert estimate_text(LoadSkillTool.description) <= 40


# ---------------------------------------------------------------------------
# G75 / G76：常驻区
# ---------------------------------------------------------------------------


def _context(*, skill_index: str = "") -> SessionContext:
    return SessionContext(
        system_prompt="基底提示词", tools_schema=[], clock=CLOCK, skill_index=skill_index
    )


def test_index_enters_the_resident_region() -> None:
    """**G75**：索引在常驻区里，且顺序变化会改变指纹。"""
    skills_a = [
        SkillMeta(name="a", description="A", location="a/a.md", source=Path("a")),
        SkillMeta(name="b", description="B", location="b/b.md", source=Path("b")),
    ]
    index = render_index(skills_a, root_label="skills/")

    with_index = _context(skill_index=index)
    without = _context()

    assert index in with_index.resident_text()
    assert with_index.fingerprint != without.fingerprint


def test_reordered_index_changes_fingerprint() -> None:
    """顺序变了 → 指纹必须变（这就是 G75 要防的"顺序不稳定"）。

    反过来看：**如果顺序稳定，指纹就稳定**——这条与下面那条是一对。
    """
    items = [
        SkillMeta(name=n, description=n, location=f"{n}/{n}.md", source=Path(n))
        for n in ("a", "b")
    ]

    forward = _context(skill_index=render_index(items, root_label="s/"))
    reverse = _context(skill_index=render_index(list(reversed(items)), root_label="s/"))

    assert forward.fingerprint != reverse.fingerprint


def test_empty_index_keeps_resident_byte_identical() -> None:
    """没有技能时，常驻区**逐字节等于**加这个功能之前。

    与 ``project_instructions`` 的处置同源：新功能不该改变既有路径的字节，
    否则"没建技能目录的项目"会平白付一次 prompt cache 失效。
    """
    assert _context().fingerprint == _context(skill_index="").fingerprint


def test_body_never_enters_the_resident_region(tmp_path: Path) -> None:
    """**G76 本体**：加载正文前后，常驻区**一个字节都不变**。

    这是渐进披露的地基。用"只在正文里出现的短语"来判——
    用索引里也有的词（比如技能名或描述）会得到一个**永远为真**的断言，
    那正是"证伪不了的断言会假绿"。
    """
    _make_skill(tmp_path, "alpha", body_only="只在正文里出现的一句话")
    scan = discover_skills(tmp_path)
    index = render_index(scan.skills, root_label="skills/")
    context = _context(skill_index=index)

    before = context.fingerprint
    body = load_body(scan.skills[0])  # 模拟工具把正文取出来

    assert "只在正文里出现的一句话" in body.text
    assert "只在正文里出现的一句话" not in context.resident_text()
    assert context.fingerprint == before, "加载正文后常驻区变了——渐进披露失效"


def test_skill_index_counts_toward_the_budget() -> None:
    """技能索引必须**算进**常驻区预算，不能是预算管不到的盲区。

    否则它就等于"偷偷往常驻区加东西"——而那正是 D4 要防的事。
    """
    plain = _context()
    with_index = _context(skill_index="可用技能：\n" + "- x：" + "很长的描述" * 100)

    assert with_index.resident_tokens > plain.resident_tokens
    from sigma_session.context import ResidentBudgetExceeded

    with pytest.raises(ResidentBudgetExceeded):
        # 索引足够大时，预算闸必须响（G59）
        SessionContext(
            system_prompt="基底",
            tools_schema=[],
            clock=CLOCK,
            skill_index="x" * 20_000,
            resident_budget_tokens=100,
        ).build_messages()


# ---------------------------------------------------------------------------
# G79：**截断说明自己不能被预算吃掉**（2026-09-22 真实踩到）
# ---------------------------------------------------------------------------

#: 档位刻意从"宽松"扫到"紧到详细标记必装不下"。
#:
#: 重点不是分界点落在哪，而是**每一档的结论都与环境无关**——
#: 跑它的机器上临时目录有多长，不该改变任何一条断言的真假。
#:
#: 下限取 25 是**测出来的，不是猜的**（2026-09-22 实测，``CHARS_PER_TOKEN = 3.0``）：
#:
#: ```text
#: 详细标记  145 字符 = 固定文案 91 + source 绝对路径 54   ← 路径那截随环境变
#: 短标记     44 字符 = 固定文案 + location 相对路径       ← 与环境无关
#: ```
#:
#: 要"详细装不下、短的一定装得下"，需 ``len(短) < 3 × max_tokens < len(详细)``。
#: 代入 25：``44 < 75 < 145`` ✓ —— **两个不等式都留了余量**（31 与 70 字符），
#: 所以这一档在任何机器上都成立 ⇒ G79 的注入（去掉短标记）必红。
#: 别把下限压到 15：45 字符的预算只比短标记多 1 个字符，那是在碰运气。
_BODY_BUDGETS = [400, 300, 200, 150, 100, 60, 40, 30, 25]


@pytest.mark.parametrize("max_tokens", _BODY_BUDGETS)
def test_discard_stays_visible_at_every_body_budget(
    tmp_path: Path, max_tokens: int
) -> None:
    """**G79 本体**：任何预算档下，「被截断」都必须是可见的。

    这条要防的是一个**真实踩到的形态**（2026-09-22，由全量跑暴露,单文件跑是绿的）：

        详细标记里带**绝对路径**，而路径长度**随环境变化**——
        临时目录在 ``AppData\\Local\\Temp`` 下约 90 字符，指进项目里就有 130+。
        于是同一个 ``max_tokens=200``：一种环境下切得下；
        另一种环境下**标记自己就把预算吃光** → 候选标记全被否 → 返回**空文本**。
        调用方拿到一段既没正文、也没说明的结果，**「丢弃」就这样静默了**。

    ``resources.py`` 修过一模一样的坑（AGENTS.md 的标记撑破预算），
    当时的处置是加短标记退路——技能这边漏了。**同一个坑第二次踩到**，
    所以这次除了修，还要把「短标记真的在接手」钉住。

    第二条断言（``result.text`` 非空）才是真正有区分力的那一条：
    把 ``load_body`` 的 ``markers`` 里的 ``short_marker`` 去掉，
    低档位必然返回空文本 → 它会红。
    """
    _make_skill(tmp_path, "big", body_only="很长的正文句子。\n" * 400)
    meta = discover_skills(tmp_path).skills[0]

    result = load_body(meta, max_tokens=max_tokens)

    assert result.truncated, "正文约 1000 token，这一档不可能不截断"
    assert result.text, (
        f"预算 {max_tokens} token 下返回了空文本。"
        "正文放不下可以接受，但**连「被截断了」都不说**不行——"
        "详细标记装不下时，短标记必须接手。"
    )
    assert result.tokens <= max_tokens, f"{result.tokens} 超上限 {max_tokens}"
    assert "截断" in result.text, f"标记里没说被截断：{result.text[:60]!r}"


def test_a_body_that_fits_gets_no_marker(tmp_path: Path) -> None:
    """反向：**没截断就不该出现「已截断」字样**。

    少了这一条，上面那个断言可以靠「永远加一句已截断」骗过去——
    而那会让模型以为读到的规范不完整，于是去 read 一遍全文，白花一轮。
    **假阳性与假阴性都要花钱。**
    """
    _make_skill(tmp_path, "small")
    meta = discover_skills(tmp_path).skills[0]

    result = load_body(meta, max_tokens=500)

    assert not result.truncated
    assert "截断" not in result.text


async def test_tool_blames_the_budget_not_the_file(tmp_path: Path) -> None:
    """预算小到放不下任何说明时，报错必须**指向预算**，不是指向文件。

    三种空文本的原因要指向三种不同的排查方向：

    | 原因 | 该去查什么 |
    | --- | --- |
    | 文件读不出来 | 权限 / 编码 / 文件在不在 |
    | 文件里只有 frontmatter | 文件内容 |
    | 预算装不下截断说明 | **预算（``max_tokens``）** |

    第一版把三种合成一句「正文读不出来（权限 / 编码 / 文件被删）」，
    于是第三种会让人**去查文件系统**——而文件好好的。症状不指向根因。
    """
    from sigma_agent.types import ToolContext
    from sigma_ai.base import NeverCancelled

    _make_skill(tmp_path, "big", body_only="很长的正文句子。\n" * 400)
    tool = LoadSkillTool(skills=discover_skills(tmp_path).skills, max_tokens=1)
    ctx = ToolContext(
        session_id="s", workspace_root=tmp_path, signal=NeverCancelled()
    )

    result = await tool.run(tool.params(name="big"), ctx)

    assert result.is_error
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "预算" in text, f"报错没提预算：{text[:80]!r}"
    assert result.details["reason"] == "预算装不下截断说明"


async def test_tool_says_body_is_empty_when_there_is_nothing_to_read(
    tmp_path: Path,
) -> None:
    """反向：**正文真的为空**时，报的是"没有正文"，不是"预算不够"。

    与上一条成对。只有一条的话，把报错分支写死成任一种都能绿——
    而那会让模型按错误的原因去重试。
    """
    from sigma_agent.types import ToolContext
    from sigma_ai.base import NeverCancelled

    path = _make_skill(tmp_path, "hollow")
    # 只留 frontmatter：正文为空（``load_body`` 会 strip 掉空白）
    path.write_text("---\nname: hollow\ndescription: 没有正文\n---\n\n", encoding="utf-8")
    tool = _tool(tmp_path)
    ctx = ToolContext(
        session_id="s", workspace_root=tmp_path, signal=NeverCancelled()
    )

    result = await tool.run(tool.params(name="hollow"), ctx)

    assert result.is_error
    assert result.details["reason"] == "正文为空"
